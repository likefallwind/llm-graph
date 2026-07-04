"""Wikidata 通道：维基自带的人类校对图谱，零 LLM。

每个语料页面对应一个 Wikidata QID，QID 之间有类型化关系（claims）。
三个用途：
- mine_edges：两个节点的 QID 之间存在 P279/P361/P737 claim -> 候选边（proposed，人工/复核裁决）
- 同 QID 仲裁：两个节点映射到同一 QID = 同一概念，比 embedding 相似度硬得多的去重信号
- relation_between：给 verify 的结构佐证提供 Wikidata 维度
"""
import json
import time

from . import db, wiki

# 只关心能映射到图谱边语义的属性
PROPS = {
    "P279": ("is_a", "「{a}」是「{b}」的子类"),
    "P361": ("part_of", "「{a}」是「{b}」的组成部分"),
    "P737": ("related_to", "「{a}」受「{b}」启发"),  # 特殊方向处理见 mine_edges
}
CONFIDENCE = 0.7  # 人类校对过的结构，比分类挖掘（0.5）可信，但语义映射仍需审


def ensure_qids(conn) -> int:
    """给所有节点已映射的语料页补 QID（带缓存，查过的不重查）。返回本次新查页数。"""
    todo = {}  # {lang: [page_id]}
    for row in conn.execute(
            "SELECT DISTINCT np.lang, np.page_id FROM node_page np"
            " LEFT JOIN page_qid pq ON pq.lang=np.lang AND pq.page_id=np.page_id"
            " WHERE pq.page_id IS NULL"):
        todo.setdefault(row["lang"], []).append(row["page_id"])
    n = 0
    for lang, pids in todo.items():
        for pid, qid in wiki.page_qids(lang, pids).items():
            conn.execute("INSERT OR REPLACE INTO page_qid(lang, page_id, qid, fetched_at)"
                         " VALUES (?,?,?,?)", (lang, pid, qid, time.time()))
            n += 1
    conn.commit()
    return n


def ensure_claims(conn) -> int:
    """给所有已知 QID 补 claims 缓存。返回本次新查实体数。"""
    qids = [r["qid"] for r in conn.execute(
        "SELECT DISTINCT pq.qid FROM page_qid pq"
        " LEFT JOIN wikidata_claims wc ON wc.qid=pq.qid"
        " WHERE pq.qid != '' AND wc.qid IS NULL")]
    if not qids:
        return 0
    for qid, claims in wiki.wikidata_claims(qids, list(PROPS)).items():
        conn.execute("INSERT OR REPLACE INTO wikidata_claims(qid, claims, fetched_at)"
                     " VALUES (?,?,?)", (qid, json.dumps(claims), time.time()))
    conn.commit()
    return len(qids)


def node_qids(conn) -> dict:
    """{node_id: qid}，仅含有 QID 的非 rejected 节点。"""
    out = {}
    for row in conn.execute(
            "SELECT n.id, pq.qid FROM nodes n"
            " JOIN node_page np ON np.node_id=n.id"
            " JOIN page_qid pq ON pq.lang=np.lang AND pq.page_id=np.page_id"
            " WHERE n.status != 'rejected' AND pq.qid != ''"):
        out[row["id"]] = row["qid"]
    return out


def _claims_of(conn, qid: str) -> dict:
    row = conn.execute("SELECT claims FROM wikidata_claims WHERE qid=?", (qid,)).fetchone()
    return json.loads(row["claims"]) if row else {}


def relation_between(conn, qid_a: str, qid_b: str) -> list[str]:
    """两个 QID 之间已缓存的 claim 关系，如 ['P279 a→b']。给 verify 佐证用。"""
    out = []
    for prop in PROPS:
        if qid_b in _claims_of(conn, qid_a).get(prop, []):
            out.append(f"{prop} a→b")
        if qid_a in _claims_of(conn, qid_b).get(prop, []):
            out.append(f"{prop} b→a")
    return out


def mine_edges(conn) -> list[str]:
    """QID 间 claims -> 候选边（proposed）；同 QID 节点对 -> 疑似同概念告警。"""
    lines = []
    n_pages = ensure_qids(conn)
    n_claims = ensure_claims(conn)
    if n_pages or n_claims:
        lines.append(f"（QID 新增 {n_pages}，claims 新增 {n_claims}）")

    nq = node_qids(conn)
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}

    # 同 QID：多个节点共用同一 Wikidata 项——可能是同一概念（该合并），
    # 也可能只是共享来源页（如前向/反向传播同页），请人工裁决；这类歧义 QID 不做边目标
    by_qid = {}
    for nid, qid in nq.items():
        by_qid.setdefault(qid, []).append(nid)
    for qid, ids in by_qid.items():
        if len(ids) > 1:
            lines.append(f"⚠ 共用 Wikidata 项 {qid}: {'、'.join(names[i] for i in ids)}"
                         f"——同一概念请合并，共享来源页则需拆分映射，请人工裁决")

    qid_to_node = {qid: ids[0] for qid, ids in by_qid.items() if len(ids) == 1}
    for nid, qid in nq.items():
        if qid_to_node.get(qid) != nid:
            continue  # 歧义 QID（多节点共用）：claims 归属不明，不做边的源头
        for prop, targets in _claims_of(conn, qid).items():
            edge_type, tmpl = PROPS[prop]
            for t in targets:
                other = qid_to_node.get(t)
                if other is None or other == nid:
                    continue
                a, b = names[nid], names[other]
                rationale = f"[Wikidata] {tmpl.format(a=a, b=b)}（{prop}）"
                if prop == "P737":
                    # A 受 B 启发 -> B 启发了 A：演化启发的方向是 启发者->被启发者
                    src, dst = other, nid
                    rationale = f"[演化启发] {rationale}"
                else:
                    src, dst = nid, other
                rowid = db.add_edge(conn, src, dst, edge_type, confidence=CONFIDENCE,
                                    rationale=rationale, source=f"wikidata:{prop}",
                                    status="proposed")
                if rowid:
                    lines.append(f"候选边: {names[src]} -{edge_type}-> {names[dst]}（{prop}）")
    conn.commit()
    return lines
