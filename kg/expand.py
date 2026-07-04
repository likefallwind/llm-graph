"""扩展 agent（假设生成器）：LLM 记忆负责“往哪看”，语料负责“什么是真的”。

M3 凭知识只提议缺口名字（不编内容）-> 去语料库/Wikipedia 找来源页：
找到 -> 转主题模式 ingest（有据提取），找不到 -> 丢弃。
LLM 记忆的不可校准性被隔离在选点环节，不进图谱内容。
"""
import json

from . import corpus, db, ingest, llm

GAP_PROMPT = """你在维护一个 AI 领域的教学知识图谱。节点是知识点，边表示 is_a / part_of / 教学先修 / 受限关联。

当前需要扩展的前沿节点：
名称：{name}
定义：{definition}
facets：{facets}
已有邻居：{neighbors}

图谱中已存在的全部节点名（不要重复提议）：
{all_names}

请提议 1~{limit} 个图谱**缺失**、教学上重要的相邻知识点（优先教学先修上的缺口）。
只报名字和缺口理由，不要编造定义和关系——系统会去权威语料验证并提取，语料里找不到的提议会被丢弃。

输出 JSON：
{{"gaps": [{{"name": "知识点名（中文优先）", "aliases": ["常见英文名/别名，帮助检索"], "why": "一句话：为什么图谱缺它"}}]}}
若该节点邻域已完备，输出 {{"gaps": []}}。"""


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
    result = llm.chat_json([{"role": "user", "content": GAP_PROMPT.format(
        name=node["name"], definition=node["definition"],
        facets=json.dumps(node["facets"], ensure_ascii=False),
        neighbors="\n".join(neighbors_of(conn, node["id"])) or "（无）",
        all_names="、".join(all_names), limit=limit)}])

    gaps = result.get("gaps", [])
    stats = {"hypotheses": len(gaps), "verified": 0, "dropped": 0,
             "proposed_nodes": 0, "merged_aliases": 0, "proposed_edges": 0,
             "dropped_related": 0, "dropped_no_evidence": 0, "details": []}

    for g in gaps:
        name = g.get("name", "").strip()
        if not name:
            continue
        existing = db.find_by_name_or_alias(conn, name)
        if existing:
            stats["details"].append(f"假设「{name}」已是节点「{existing['name']}」，跳过")
            continue
        # 语料验证：本地语料 -> 维基检索（name 和 aliases 都试）
        page = corpus.find_page(conn, name)
        for q in [name] + g.get("aliases", []) if not page else []:
            page = corpus.fetch_and_store(conn, q)
            if page:
                break
        if not page:
            stats["dropped"] += 1
            stats["details"].append(f"✗ 假设「{name}」语料无来源，丢弃（理由曾是：{g.get('why', '')}）")
            continue
        stats["verified"] += 1
        stats["details"].append(f"✓ 假设「{name}」验证到来源 {corpus.source_of(page)}，转有据提取")
        if dry_run:
            continue
        try:
            sub = ingest.ingest_hypothesis(conn, name, page)
        except RuntimeError as exc:
            stats["details"].append(f"  「{name}」提取失败（{exc}），跳过")
            continue
        new_node = db.find_by_name_or_alias(conn, name)
        if new_node:
            corpus.map_node(conn, new_node["id"], page)
        for key in ("proposed_nodes", "merged_aliases", "proposed_edges",
                    "dropped_related", "dropped_no_evidence"):
            stats[key] += sub[key]
        stats["details"] += ["  " + line for line in sub["details"]]
    return stats
