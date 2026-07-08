"""SQLite 存储层：节点、边、embedding。"""
import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("KG_DB", os.path.join(os.path.dirname(__file__), "..", "data", "kg.db"))

EDGE_TYPES = ("is_a", "part_of", "prerequisite_of", "related_to")
# related_to 只允许这三种教学上有价值的情形，泛泛的"同领域相关"不许连边
RELATED_KINDS = ("同题替代", "演化启发", "教学对比")
# 误区（Misconception）作为带前缀的特殊 facet 存储（如「误区:更深的网络一定更好」），
# 走 ingest 全部约束（有据提取、evidence 校验）；学习者层落地后再考虑升独立类型
MISCONCEPTION_PREFIX = "误区:"
NODE_STATUS = ("seed", "proposed", "approved", "rejected")
EDGE_STATUS = ("seed", "proposed", "approved", "rejected")
# 节点类型：现在只有 concept 一种；误区/题目/资源升独立类型时在此登记，
# 并在 EDGE_ENDPOINT_TYPES 里补对应端点规则（如误区不能做先修边端点）
NODE_TYPES = ("concept",)
# 边类型 × 端点节点类型合法矩阵：{边类型: (允许的 src 类型, 允许的 dst 类型)}，
# add_edge 写入前校验，guards.bad_edge_endpoints 对存量数据兜底
EDGE_ENDPOINT_TYPES = {
    "is_a": (("concept",), ("concept",)),
    "part_of": (("concept",), ("concept",)),
    "prerequisite_of": (("concept",), ("concept",)),
    "related_to": (("concept",), ("concept",)),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    type       TEXT NOT NULL DEFAULT 'concept',
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
    action     TEXT NOT NULL,             -- approve/reject/merge/flip/retype/demote/audit_*/rollback
    detail     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',  -- 条目的 source，按通道统计 precision 用
    decided_by TEXT NOT NULL DEFAULT 'human',  -- human / auto
    batch_id   TEXT NOT NULL DEFAULT '',  -- verify --apply 一次运行一个批次，坏批次可整批回滚
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
CREATE TABLE IF NOT EXISTS doc_sections (
    id            INTEGER PRIMARY KEY,
    book          TEXT NOT NULL,             -- 源 slug（[a-z0-9-]，source 解析依赖）
    ord           INTEGER NOT NULL,          -- 全书章节序，toc 先修信号的来源
    sec_id        TEXT NOT NULL,             -- 章节号，如 "3.1"
    title         TEXT NOT NULL,
    url           TEXT NOT NULL DEFAULT '',
    orig_lang     TEXT NOT NULL,             -- zh / en
    orig_text     TEXT NOT NULL DEFAULT '',  -- 原文快照（溯源；zh 源与 text 相同）
    text          TEXT NOT NULL DEFAULT '',  -- 中文正文（en 源为翻译；'' = 未翻译）
    content_hash  TEXT NOT NULL DEFAULT '',  -- text 的 sha256 前 12 位，source 的 @ 版本号
    fetched_at    REAL,
    translated_at REAL,
    UNIQUE(book, sec_id)
);
"""


def connect(path: str = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """已有库的增量迁移（SCHEMA 的 CREATE IF NOT EXISTS 只对新库生效）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_log)")}
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE review_log ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "type" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN type TEXT NOT NULL DEFAULT 'concept'")
        conn.commit()


def node_dict(row) -> dict:
    d = dict(row)
    d["aliases"] = json.loads(d["aliases"])
    d["facets"] = json.loads(d["facets"])
    if d.get("embedding"):
        d["embedding"] = json.loads(d["embedding"])
    return d


def add_node(conn, name, definition="", aliases=None, facets=None,
             status="proposed", source="", embedding=None, type_="concept") -> int:
    assert type_ in NODE_TYPES, f"未知节点类型: {type_}"
    cur = conn.execute(
        "INSERT INTO nodes(name, type, aliases, definition, facets, status, source, embedding, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (name.strip(), type_, json.dumps(aliases or [], ensure_ascii=False), definition.strip(),
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
    """同向重复靠 UNIQUE(src,dst,type) 的 INSERT OR IGNORE；related_to 语义对称，
    反向已存在（未拒绝）也视为重复。两种情形都返回 0，调用方按 falsy 判断跳过。"""
    assert type_ in EDGE_TYPES, f"未知边类型: {type_}"
    src_ok, dst_ok = EDGE_ENDPOINT_TYPES[type_]
    types = conn.execute(
        "SELECT (SELECT type FROM nodes WHERE id=?), (SELECT type FROM nodes WHERE id=?)",
        (src, dst)).fetchone()
    assert types[0] in src_ok and types[1] in dst_ok, \
        f"边 {type_} 端点节点类型不合法: src={types[0]} dst={types[1]}"
    if type_ == "related_to":
        dup = conn.execute(
            "SELECT id FROM edges WHERE src=? AND dst=? AND type='related_to'"
            " AND status != 'rejected'", (dst, src)).fetchone()
        if dup:
            return 0
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
               detail="", source="", decided_by="human", batch_id=""):
    """裁决留痕：这是日后校准 AI 审核（按通道统计 precision）的标注数据，每次裁决都要记。
    batch_id 标记 verify --apply 的运行批次，坏批次可 kg rollback 整批撤销。"""
    conn.execute(
        "INSERT INTO review_log(item_type, item_id, action, detail, source, decided_by, batch_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (item_type, item_id, action, detail, source, decided_by, batch_id, time.time()))


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
