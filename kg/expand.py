"""扩展 agent：选前沿节点 -> M3 提议邻居 -> 去重 -> proposed 入库。"""
import json

from . import db, dedup, llm

PROPOSE_PROMPT = """你在维护一个 AI 领域的教学知识图谱。节点是知识点，边有 4 种：
- is_a：A 是 B 的一种（CNN is_a 神经网络）
- part_of：A 是 B 的组成部分（神经元 part_of 神经网络）
- prerequisite_of：学 B 之前必须先懂 A（教学先修，最重要）
- related_to：弱关联

节点粒度规则：一个细节默认作为父节点的 facet（字符串），只有当别的知识点单独依赖它时才值得成为独立节点。

当前需要扩展的节点：
名称：{name}
定义：{definition}
facets：{facets}
已有邻居：{neighbors}

图谱中已存在的全部节点名（不要重复提议）：
{all_names}

请提议 1~{limit} 个应当补充的相邻知识点（优先补教学上缺失的先修概念），输出 JSON：
{{"proposals": [
  {{
    "name": "知识点名（中文优先，英文术语放 aliases）",
    "aliases": ["..."],
    "definition": "一句话定义",
    "facets": ["该知识点内部的细节侧面，可为空"],
    "edges": [
      {{"other": "已有节点名或本次提议的节点名",
        "type": "is_a|part_of|prerequisite_of|related_to",
        "direction": "new_to_other 或 other_to_new",
        "confidence": 0.0~1.0,
        "rationale": "一句话理由"}}
    ]
  }}
]}}
若该节点邻域已完备，输出 {{"proposals": []}}。"""


def pick_frontier(conn, k=1):
    """选边最稀疏、最新加入的生效节点作为扩展前沿。"""
    nodes = [n for n in db.list_nodes(conn) if n["status"] in db.visible_statuses()]
    nodes.sort(key=lambda n: (db.degree(conn, n["id"]), -n["created_at"]))
    return nodes[:k]


def neighbors_of(conn, node_id: int):
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    out = []
    for e in db.approved_edges(conn):
        if e["src"] == node_id:
            out.append(f"{names[node_id]} -{e['type']}-> {names[e['dst']]}")
        elif e["dst"] == node_id:
            out.append(f"{names[e['src']]} -{e['type']}-> {names[node_id]}")
    return out


def expand_node(conn, node: dict, limit=5, dry_run=False) -> dict:
    all_names = [n["name"] for n in db.list_nodes(conn) if n["status"] != "rejected"]
    prompt = PROPOSE_PROMPT.format(
        name=node["name"], definition=node["definition"],
        facets=json.dumps(node["facets"], ensure_ascii=False),
        neighbors="\n".join(neighbors_of(conn, node["id"])) or "（无）",
        all_names="、".join(all_names), limit=limit)

    result = llm.chat_json([{"role": "user", "content": prompt}], max_tokens=16384)
    proposals = result.get("proposals", [])
    stats = {"proposed_nodes": 0, "merged_aliases": 0, "proposed_edges": 0, "details": []}
    if dry_run:
        stats["details"] = proposals
        return stats

    name_to_id = {}
    for p in proposals:
        name, definition = p.get("name", "").strip(), p.get("definition", "")
        if not name:
            continue
        existing, how = dedup.find_duplicate(conn, name, definition)
        if existing:
            dedup.merge_as_alias(conn, existing, name)
            name_to_id[name] = existing["id"]
            stats["merged_aliases"] += 1
            stats["details"].append(f"「{name}」与已有节点「{existing['name']}」重复（{how}），合并为别名")
            continue
        embedding = how  # find_duplicate 未命中时第二个返回值是已算好的向量
        node_id = db.add_node(conn, name, definition=definition,
                              aliases=p.get("aliases", []), facets=p.get("facets", []),
                              status="proposed", source=f"expand:{node['name']}",
                              embedding=embedding)
        name_to_id[name] = node_id
        stats["proposed_nodes"] += 1
        stats["details"].append(f"新提议节点「{name}」")

    for p in proposals:
        src_of_new = name_to_id.get(p.get("name", "").strip())
        if src_of_new is None:
            continue
        for e in p.get("edges", []):
            other = db.find_by_name_or_alias(conn, e.get("other", ""))
            other_id = other["id"] if other else name_to_id.get(e.get("other", "").strip())
            if other_id is None or other_id == src_of_new:
                continue
            if e.get("direction") == "other_to_new":
                src, dst = other_id, src_of_new
            else:
                src, dst = src_of_new, other_id
            if e.get("type") not in db.EDGE_TYPES:
                continue
            rowid = db.add_edge(conn, src, dst, e["type"],
                                confidence=float(e.get("confidence", 0.5)),
                                rationale=e.get("rationale", ""),
                                source=f"expand:{node['name']}", status="proposed")
            if rowid:
                stats["proposed_edges"] += 1
    conn.commit()
    return stats
