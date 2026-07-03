"""一致性守卫：先修环检测、孤儿节点、facet 升级提示。"""
from collections import defaultdict

from . import db


def prereq_cycles(conn):
    """在生效的 prerequisite_of 子图上找环，返回环的节点名列表。"""
    adj = defaultdict(list)
    for e in db.approved_edges(conn, "prerequisite_of"):
        adj[e["src"]].append(e["dst"])

    WHITE, GRAY, BLACK = 0, 1, 2
    color, cycles = defaultdict(int), []

    def dfs(u, path):
        color[u] = GRAY
        path.append(u)
        for v in adj[u]:
            if color[v] == GRAY:
                cycles.append(path[path.index(v):] + [v])
            elif color[v] == WHITE:
                dfs(v, path)
        path.pop()
        color[u] = BLACK

    for u in list(adj):
        if color[u] == WHITE:
            dfs(u, [])

    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    return [[names.get(i, str(i)) for i in cyc] for cyc in cycles]


def orphans(conn):
    """无任何生效边的生效节点。"""
    connected = set()
    for e in db.approved_edges(conn):
        connected.add(e["src"])
        connected.add(e["dst"])
    result = []
    for n in db.list_nodes(conn):
        if n["status"] in db.visible_statuses() and n["id"] not in connected:
            result.append(n["name"])
    return result


def facet_shadows(conn):
    """facet 文本与某个已有节点重名——提示该 facet 可能已被升级为节点，应从父节点移除。"""
    all_names = {}
    for n in db.list_nodes(conn):
        if n["status"] != "rejected":
            all_names[n["name"].lower()] = n["name"]
            for a in n["aliases"]:
                all_names[a.lower()] = n["name"]
    hits = []
    for n in db.list_nodes(conn):
        if n["status"] == "rejected":
            continue
        for f in n["facets"]:
            target = all_names.get(f.lower())
            if target and target != n["name"]:
                hits.append((n["name"], f, target))
    return hits


def run_all(conn) -> str:
    lines = []
    cycles = prereq_cycles(conn)
    if cycles:
        lines.append("⚠ 先修子图存在环（必有错边，请裁决）：")
        lines.extend("  " + " -> ".join(c) for c in cycles)
    else:
        lines.append("✓ 先修子图无环")
    orph = orphans(conn)
    if orph:
        lines.append(f"⚠ 孤儿节点 {len(orph)} 个: " + ", ".join(orph))
    else:
        lines.append("✓ 无孤儿节点")
    shadows = facet_shadows(conn)
    if shadows:
        lines.append("⚠ facet 与节点重名（考虑从父节点 facets 中移除）：")
        lines.extend(f"  {parent} 的 facet「{facet}」≈ 节点「{node}」" for parent, facet, node in shadows)
    else:
        lines.append("✓ 无 facet/节点重名")
    return "\n".join(lines)
