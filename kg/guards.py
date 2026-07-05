"""一致性守卫：层级边环检测、先修传递冗余、related_to 反向对、孤儿节点、facet 升级提示。

守卫只报告不自动修——裁决权在人。
"""
from collections import defaultdict

from . import db

# 出环必有错边的边类型：先修（教学顺序）、is_a / part_of（层级）
ACYCLIC_TYPES = ("prerequisite_of", "is_a", "part_of")


def cycles(conn, edge_type: str):
    """在生效的指定类型子图上找环，返回环的节点名列表。"""
    adj = defaultdict(list)
    for e in db.approved_edges(conn, edge_type):
        adj[e["src"]].append(e["dst"])

    WHITE, GRAY, BLACK = 0, 1, 2
    color, found = defaultdict(int), []

    def dfs(u, path):
        color[u] = GRAY
        path.append(u)
        for v in adj[u]:
            if color[v] == GRAY:
                found.append(path[path.index(v):] + [v])
            elif color[v] == WHITE:
                dfs(v, path)
        path.pop()
        color[u] = BLACK

    for u in list(adj):
        if color[u] == WHITE:
            dfs(u, [])

    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    return [[names.get(i, str(i)) for i in cyc] for cyc in found]


def prereq_cycles(conn):
    """兼容旧入口：生效先修子图上的环。"""
    return cycles(conn, "prerequisite_of")


def prereq_redundant(conn):
    """先修传递冗余：直连边 A->C 存在绕开该边的更长路径 A->...->C 时报告。
    冗余直连会搅乱教学路径（先修链导出），可考虑人工拒绝直连边。"""
    adj = defaultdict(set)
    for e in db.approved_edges(conn, "prerequisite_of"):
        adj[e["src"]].add(e["dst"])
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    hits = []
    for a in list(adj):
        for c in adj[a]:
            # 从 A 出发、跳过直连边 (A,C)，BFS 能否到 C
            seen, queue = {a}, [x for x in adj[a] if x != c]
            reached = False
            while queue and not reached:
                u = queue.pop()
                if u in seen:
                    continue
                seen.add(u)
                if c in adj[u]:
                    reached = True
                    break
                queue.extend(adj[u] - seen)
            if reached:
                hits.append((names.get(a, str(a)), names.get(c, str(c))))
    return hits


def mutual_edges(conn):
    """生效边中 A->B 与 B->A 同类型并存的对（related_to 语义对称必为重复；
    其他类型互指必有一条方向错）。"""
    rows = conn.execute(
        "SELECT a.type t, a.src, a.dst FROM edges a JOIN edges b"
        " ON a.src=b.dst AND a.dst=b.src AND a.type=b.type AND a.id<b.id"
        " WHERE a.status IN ('seed','approved') AND b.status IN ('seed','approved')").fetchall()
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    return [(r["t"], names.get(r["src"], "?"), names.get(r["dst"], "?")) for r in rows]


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
    for t in ACYCLIC_TYPES:
        cyc = cycles(conn, t)
        if cyc:
            lines.append(f"⚠ {t} 子图存在环（必有错边，请裁决）：")
            lines.extend("  " + " -> ".join(c) for c in cyc)
        else:
            lines.append(f"✓ {t} 子图无环")
    redundant = prereq_redundant(conn)
    if redundant:
        lines.append(f"⚠ 先修传递冗余 {len(redundant)} 条（已有间接路径，考虑拒绝直连边）：")
        lines.extend(f"  {a} -> {c}" for a, c in redundant)
    else:
        lines.append("✓ 无先修传递冗余")
    mutual = mutual_edges(conn)
    if mutual:
        lines.append("⚠ 正反向同型边并存（related_to 为重复，其他类型必有一条方向错）：")
        lines.extend(f"  {a} <-{t}-> {b}" for t, a, b in mutual)
    else:
        lines.append("✓ 无正反向同型边")
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
