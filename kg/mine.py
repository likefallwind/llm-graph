"""结构挖掘：从语料库的维基自带结构提取免费标注，零 LLM。

- 重定向表 -> 节点别名（人工维护的同义词典，直接生效）
- 分类树   -> part_of 候选边（proposed，人工审核）
- 内链统计 -> 选点分数（见 corpus.link_counts / ingest.pick_anchors）
"""
from . import corpus, db

MAX_ALIAS_CHARS = 25  # 过长的重定向名多是描述性短语，不当别名


def import_aliases(conn) -> list[str]:
    """把每个节点对应页面的重定向名合并为别名。返回日志行。"""
    lines = []
    index = corpus.title_index(conn)
    # 全局名字表，检测重定向撞上别的节点（提示人工合并，不自动加）
    owner = {}
    for n in db.list_nodes(conn):
        if n["status"] == "rejected":
            continue
        owner[n["name"].lower()] = n["name"]
        for a in n["aliases"]:
            owner.setdefault(a.lower(), n["name"])

    for node in db.list_nodes(conn):
        if node["status"] == "rejected":
            continue
        page = corpus.page_for_node(conn, node, index, with_text=False)
        if not page:
            continue
        added = []
        for r in page["redirects"]:
            r = r.strip()
            low = r.lower()
            if not r or len(r) > MAX_ALIAS_CHARS:
                continue
            if low == node["name"].lower() or low in (a.lower() for a in node["aliases"] + added):
                continue
            if low in owner and owner[low] != node["name"]:
                lines.append(f"⚠ 「{r}」重定向到「{node['name']}」的页面，"
                             f"但已是节点「{owner[low]}」的名字/别名——两个节点可能是同一概念，请人工裁决")
                continue
            added.append(r)
            owner[low] = node["name"]
        if added:
            db.update_node(conn, node["id"], aliases=node["aliases"] + added)
            lines.append(f"「{node['name']}」+别名: {', '.join(added)}")
    conn.commit()
    return lines


def category_edges(conn) -> list[str]:
    """页面分类名命中已有节点 -> part_of 候选边（proposed，低置信度）。"""
    lines = []
    index = corpus.title_index(conn)
    for node in db.list_nodes(conn):
        if node["status"] == "rejected":
            continue
        page = corpus.page_for_node(conn, node, index, with_text=False)
        if not page:
            continue
        for cat in page["categories"]:
            target = db.find_by_name_or_alias(conn, cat)
            if not target or target["id"] == node["id"]:
                continue
            rowid = db.add_edge(
                conn, node["id"], target["id"], "part_of", confidence=0.5,
                rationale=f"[分类挖掘] 页面《{page['title']}》属于维基分类「{cat}」",
                source=f"mine:category:{page['lang']}", status="proposed")
            if rowid:
                lines.append(f"候选边: {node['name']} -part_of-> {target['name']}（分类「{cat}」）")
    conn.commit()
    return lines
