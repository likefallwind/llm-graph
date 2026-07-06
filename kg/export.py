"""为将来的 LLM 教学系统预留的接口：节点邻域导出为结构化 JSON。"""
from . import corpus, db, docs


def resources(conn, node: dict) -> list[dict]:
    """该概念在哪些语料资源有讲解（零 LLM，复用节点↔语料映射）：
    维基页（node_page/标题匹配）+ 各教材的对应章节（标题命中优先，其次正文提及）。
    未来教学系统据此推荐阅读材料，顺序即推荐优先级：教材标题命中 > 维基 > 教材正文提及。"""
    docs_hit = []
    for book in sorted({s["book"] for s in docs.sections(conn)}):
        sec, how = docs.section_for_node(conn, node, book, with_how=True)
        if sec:
            docs_hit.append({"type": "doc", "book": book, "sec_id": sec["sec_id"],
                             "title": sec["title"], "url": docs.url_of(sec), "how": how})
    out = [r for r in docs_hit if r["how"] == "title"]
    page = corpus.page_for_node(conn, node, with_text=False)
    if page:
        out.append({"type": "wiki", "lang": page["lang"], "title": page["title"],
                    "url": corpus.url_of(page)})
    return out + [r for r in docs_hit if r["how"] != "title"]


def neighborhood(conn, name: str) -> dict:
    """定义 + facets/误区 + 先修链（向上到根）+ 直接下游 + 讲解资源。"""
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

    # 误区是带前缀的特殊 facet，导出时拆开：教学系统对二者的用法完全不同
    # （facet 是讲解侧面，误区是要主动预防/诊断的错误认识）
    plain = [f for f in node["facets"] if not f.startswith(db.MISCONCEPTION_PREFIX)]
    misconceptions = [f[len(db.MISCONCEPTION_PREFIX):] for f in node["facets"]
                      if f.startswith(db.MISCONCEPTION_PREFIX)]

    return {
        "name": node["name"],
        "aliases": node["aliases"],
        "definition": node["definition"],
        "facets": plain,
        "misconceptions": misconceptions,
        "direct_prerequisites": [names[i] for i in prereq_in.get(node["id"], [])],
        "prerequisite_chain": chain,
        "unlocks": downstream,
        "taxonomy": taxonomy,
        "resources": resources(conn, node),
    }
