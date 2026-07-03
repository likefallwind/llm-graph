"""种子导入：YAML -> 数据库（source=seed，直接生效）。"""
import yaml

from . import db, llm


def load(conn, path: str, with_embeddings=True) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    source = data.get("source", "seed")
    stats = {"nodes": 0, "nodes_skipped": 0, "edges": 0, "edges_skipped": 0}

    for spec in data.get("nodes", []):
        name = spec["name"].strip()
        if db.find_by_name_or_alias(conn, name):
            stats["nodes_skipped"] += 1
            continue
        db.add_node(conn, name,
                    definition=spec.get("definition", ""),
                    aliases=spec.get("aliases", []),
                    facets=spec.get("facets", []),
                    status="seed", source=source)
        stats["nodes"] += 1

    for spec in data.get("edges", []):
        src_name, type_, dst_name = spec[0], spec[1], spec[2]
        confidence = spec[3] if len(spec) > 3 else 1.0
        src = db.find_by_name_or_alias(conn, src_name)
        dst = db.find_by_name_or_alias(conn, dst_name)
        if not src or not dst:
            missing = src_name if not src else dst_name
            print(f"  跳过边 {spec[:3]}：节点「{missing}」不存在")
            stats["edges_skipped"] += 1
            continue
        rowid = db.add_edge(conn, src["id"], dst["id"], type_,
                            confidence=confidence, source=source, status="seed")
        stats["edges" if rowid else "edges_skipped"] += 1

    conn.commit()

    if with_embeddings:
        ensure_embeddings(conn)
    return stats


def ensure_embeddings(conn):
    """给还没有 embedding 的非 rejected 节点补向量（name + definition）。"""
    todo = [n for n in db.list_nodes(conn)
            if n["status"] != "rejected" and not n.get("embedding")]
    if not todo:
        return 0
    texts = [f"{n['name']}：{n['definition']}" for n in todo]
    vectors = llm.embed(texts, kind="db")
    for n, v in zip(todo, vectors):
        db.update_node(conn, n["id"], embedding=v)
    conn.commit()
    return len(todo)
