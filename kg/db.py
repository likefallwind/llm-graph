"""SQLite 存储层：节点、边、embedding。"""
import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("KG_DB", os.path.join(os.path.dirname(__file__), "..", "data", "kg.db"))

EDGE_TYPES = ("is_a", "part_of", "prerequisite_of", "related_to")
# related_to 只允许这三种教学上有价值的情形，泛泛的"同领域相关"不许连边
RELATED_KINDS = ("同题替代", "演化启发", "教学对比")
NODE_STATUS = ("seed", "proposed", "approved", "rejected")
EDGE_STATUS = ("seed", "proposed", "approved", "rejected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    aliases    TEXT NOT NULL DEFAULT '[]',
    definition TEXT NOT NULL DEFAULT '',
    facets     TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'proposed',
    source     TEXT NOT NULL DEFAULT '',
    embedding  TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    id         INTEGER PRIMARY KEY,
    src        INTEGER NOT NULL REFERENCES nodes(id),
    dst        INTEGER NOT NULL REFERENCES nodes(id),
    type       TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    rationale  TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'proposed',
    created_at REAL NOT NULL,
    UNIQUE(src, dst, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS corpus (
    id          INTEGER PRIMARY KEY,
    lang        TEXT NOT NULL,
    page_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    revision_id INTEGER NOT NULL,
    text        TEXT NOT NULL,
    redirects   TEXT NOT NULL DEFAULT '[]',
    categories  TEXT NOT NULL DEFAULT '[]',
    links       TEXT NOT NULL DEFAULT '[]',
    fetched_at  REAL NOT NULL,
    UNIQUE(lang, page_id),
    UNIQUE(lang, title)
);
CREATE TABLE IF NOT EXISTS node_page (
    node_id INTEGER PRIMARY KEY REFERENCES nodes(id),
    lang    TEXT NOT NULL,
    page_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_log (
    id         INTEGER PRIMARY KEY,
    anchor     TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(anchor, source)
);
CREATE TABLE IF NOT EXISTS review_log (
    id         INTEGER PRIMARY KEY,
    item_type  TEXT NOT NULL,             -- node / edge
    item_id    INTEGER NOT NULL,
    action     TEXT NOT NULL,             -- approve/reject/merge/flip/retype/demote/audit_*
    detail     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',  -- 条目的 source，按通道统计 precision 用
    decided_by TEXT NOT NULL DEFAULT 'human',  -- human / auto
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_signals (
    item_type   TEXT NOT NULL,
    item_id     INTEGER NOT NULL,
    signals     TEXT NOT NULL DEFAULT '{}',  -- 结构佐证（互链/RefD/Wikidata），零 LLM
    llm_verdict TEXT,                        -- LLM 复核结论
    llm_reason  TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (item_type, item_id)
);
CREATE TABLE IF NOT EXISTS page_qid (
    lang       TEXT NOT NULL,
    page_id    INTEGER NOT NULL,
    qid        TEXT NOT NULL DEFAULT '',  -- '' = 查过但页面无 Wikidata 项
    fetched_at REAL NOT NULL,
    PRIMARY KEY (lang, page_id)
);
CREATE TABLE IF NOT EXISTS wikidata_claims (
    qid        TEXT PRIMARY KEY,
    claims     TEXT NOT NULL DEFAULT '{}',  -- {属性: [目标QID]}，只存我们关心的属性
    fetched_at REAL NOT NULL
);
"""


def connect(path: str = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def node_dict(row) -> dict:
    d = dict(row)
    d["aliases"] = json.loads(d["aliases"])
    d["facets"] = json.loads(d["facets"])
    if d.get("embedding"):
        d["embedding"] = json.loads(d["embedding"])
    return d


def add_node(conn, name, definition="", aliases=None, facets=None,
             status="proposed", source="", embedding=None) -> int:
    cur = conn.execute(
        "INSERT INTO nodes(name, aliases, definition, facets, status, source, embedding, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (name.strip(), json.dumps(aliases or [], ensure_ascii=False), definition.strip(),
         json.dumps(facets or [], ensure_ascii=False), status, source,
         json.dumps(embedding) if embedding else None, time.time()))
    return cur.lastrowid


def get_node(conn, name_or_id):
    if isinstance(name_or_id, int):
        row = conn.execute("SELECT * FROM nodes WHERE id=?", (name_or_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM nodes WHERE name=?", (name_or_id.strip(),)).fetchone()
    return node_dict(row) if row else None


def find_by_name_or_alias(conn, name: str):
    """精确名 / 别名匹配（不区分大小写）。"""
    name = name.strip()
    row = conn.execute("SELECT * FROM nodes WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return node_dict(row)
    lowered = name.lower()
    for row in conn.execute("SELECT * FROM nodes WHERE status != 'rejected'"):
        aliases = [a.lower() for a in json.loads(row["aliases"])]
        if lowered in aliases:
            return node_dict(row)
    return None


def list_nodes(conn, status=None):
    if status:
        rows = conn.execute("SELECT * FROM nodes WHERE status=? ORDER BY id", (status,))
    else:
        rows = conn.execute("SELECT * FROM nodes ORDER BY id")
    return [node_dict(r) for r in rows]


def update_node(conn, node_id: int, **fields):
    for k in ("aliases", "facets"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    if "embedding" in fields and not isinstance(fields["embedding"], (str, type(None))):
        fields["embedding"] = json.dumps(fields["embedding"])
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE nodes SET {sets} WHERE id=?", (*fields.values(), node_id))


def add_edge(conn, src: int, dst: int, type_: str, confidence=1.0,
             rationale="", source="", status="proposed") -> int:
    assert type_ in EDGE_TYPES, f"未知边类型: {type_}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO edges(src, dst, type, confidence, rationale, source, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (src, dst, type_, confidence, rationale, source, status, time.time()))
    return cur.lastrowid


def list_edges(conn, status=None, type_=None):
    q, args = "SELECT * FROM edges WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status)
    if type_:
        q += " AND type=?"
        args.append(type_)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def visible_statuses():
    return ("seed", "approved")


def log_review(conn, item_type: str, item_id: int, action: str,
               detail="", source="", decided_by="human"):
    """裁决留痕：这是日后校准 AI 审核（按通道统计 precision）的标注数据，每次裁决都要记。"""
    conn.execute(
        "INSERT INTO review_log(item_type, item_id, action, detail, source, decided_by, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (item_type, item_id, action, detail, source, decided_by, time.time()))


def get_signals(conn, item_type: str, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM review_signals WHERE item_type=? AND item_id=?",
                       (item_type, item_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["signals"] = json.loads(d["signals"])
    return d


def save_signals(conn, item_type: str, item_id: int, signals: dict = None,
                 llm_verdict: str = None, llm_reason: str = None):
    """写入/更新佐证信号；signals 与 llm_* 可分别更新，互不覆盖。"""
    row = get_signals(conn, item_type, item_id)
    merged = (row or {}).get("signals", {})
    if signals is not None:
        merged.update(signals)
    conn.execute(
        "INSERT OR REPLACE INTO review_signals(item_type, item_id, signals, llm_verdict, llm_reason, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (item_type, item_id, json.dumps(merged, ensure_ascii=False),
         llm_verdict if llm_verdict is not None else (row or {}).get("llm_verdict"),
         llm_reason if llm_reason is not None else (row or {}).get("llm_reason"),
         time.time()))


def approved_edges(conn, type_=None):
    """seed 与 approved 视为生效。"""
    q = "SELECT * FROM edges WHERE status IN ('seed','approved')"
    args = []
    if type_:
        q += " AND type=?"
        args.append(type_)
    return [dict(r) for r in conn.execute(q, args)]


def degree(conn, node_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM edges WHERE (src=? OR dst=?) AND status IN ('seed','approved')",
        (node_id, node_id)).fetchone()
    return row["c"]
