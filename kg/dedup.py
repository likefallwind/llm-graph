"""去重流水线：名称/别名精确匹配 -> embedding 相似度 -> LLM 裁决。"""
from . import db, llm

SIM_THRESHOLD = 0.90


def find_duplicate(conn, name: str, definition: str, embedding=None):
    """返回 (已存在节点, 方式) 或 (None, embedding)。

    embedding 参数可复用已算好的向量，避免重复调用 API。
    """
    hit = db.find_by_name_or_alias(conn, name)
    if hit:
        return hit, "exact"

    if embedding is None:
        embedding = llm.embed([f"{name}：{definition}"], kind="query")[0]

    candidates = []
    for n in db.list_nodes(conn):
        if n["status"] == "rejected" or not n.get("embedding"):
            continue
        sim = llm.cosine(embedding, n["embedding"])
        if sim >= SIM_THRESHOLD:
            candidates.append((sim, n))
    candidates.sort(key=lambda x: -x[0])

    for sim, n in candidates[:3]:
        if _llm_same_concept(name, definition, n):
            return n, f"embedding({sim:.2f})+llm"

    return None, embedding


def _llm_same_concept(name: str, definition: str, existing: dict) -> bool:
    answer = llm.chat_json([
        {"role": "system", "content": "你是知识图谱的实体消解助手。判断两个词条是否指同一个概念（同义词、译名、缩写都算同一概念）。只输出 JSON。"},
        {"role": "user", "content": (
            f"词条A：{name}（{definition}）\n"
            f"词条B：{existing['name']}（{existing['definition']}）"
            f"，别名：{', '.join(existing['aliases']) or '无'}\n\n"
            '输出：{"same": true/false, "reason": "一句话理由"}')},
    ], max_tokens=4096)
    return bool(answer.get("same"))


def merge_as_alias(conn, existing: dict, alias: str):
    """把重复名合并为已有节点的别名。"""
    if alias.strip().lower() == existing["name"].lower():
        return
    aliases = existing["aliases"]
    if alias not in aliases:
        aliases.append(alias)
        db.update_node(conn, existing["id"], aliases=aliases)
        conn.commit()
