"""为将来的 LLM 教学系统预留的接口：节点邻域导出为结构化 JSON。"""
from . import db


def neighborhood(conn, name: str) -> dict:
    """定义 + facets + 先修链（向上到根）+ 直接下游。"""
    node = db.find_by_name_or_alias(conn, name)
    if not node:
        raise KeyError(f"节点不存在: {name}")
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}

    prereq_in = {}   # dst -> [src...]
    for e in db.approved_edges(conn, "prerequisite_of"):
        prereq_in.setdefault(e["dst"], []).append(e["src"])

    # 先修链：BFS 向上收集所有祖先（去重、防环兜底）
    chain, seen, queue = [], {node["id"]}, list(prereq_in.get(node["id"], []))
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        chain.append(names[nid])
        queue.extend(prereq_in.get(nid, []))

    downstream, taxonomy = [], []
    for e in db.approved_edges(conn):
        if e["type"] == "prerequisite_of" and e["src"] == node["id"]:
            downstream.append(names[e["dst"]])
        elif e["type"] in ("is_a", "part_of") and node["id"] in (e["src"], e["dst"]):
            taxonomy.append(f"{names[e['src']]} -{e['type']}-> {names[e['dst']]}")

    return {
        "name": node["name"],
        "aliases": node["aliases"],
        "definition": node["definition"],
        "facets": node["facets"],
        "direct_prerequisites": [names[i] for i in prereq_in.get(node["id"], [])],
        "prerequisite_chain": chain,
        "unlocks": downstream,
        "taxonomy": taxonomy,
    }
