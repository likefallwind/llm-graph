"""复核层：给 proposed 条目积累独立佐证，支撑"人工审核 -> AI 为主 + 人工抽检"的过渡。

两级信号，写入 review_signals，review 时展示：
- 结构佐证（零 LLM）：语料页互链、RefD 式先修方向信号、Wikidata claim。
  这些是人类校对过的结构，与 LLM 提取相互独立。
- LLM 判断题复核：与提取不同的视角——只给材料（evidence、来源节选、双方定义），
  只回答是/否，禁止用模型记忆。复核时 LLM 也不是知识源，语料才是。

--apply 自动裁决规则（保守，只动两端已生效的边，节点一律留人工）：
- 批准：LLM 判「支持」且方向无疑 + 至少一项结构佐证 + 先修方向信号不反对
- 拒绝：LLM 判「不支持」且无任何结构佐证
其余留人工。自动裁决记 review_log(decided_by='auto')，用 kg review --audit 抽检。
"""
import re

from . import corpus, db, llm, wikidata

EDGE_TYPE_SEMANTICS = {
    "is_a": "src 是 dst 的一种",
    "part_of": "src 是 dst 的组成部分",
    "prerequisite_of": "不懂 src 就无法理解 dst（教学先修）",
    "related_to": "仅限三种情形：同题替代（解决同一问题的两种方法）/演化启发（src 直接启发 dst）/教学对比",
}

EDGE_JUDGE_PROMPT = """你是知识图谱的独立复核员。只依据下面给出的材料判断，禁止使用你自己的记忆知识；材料不足就答「证据不足」。

待复核断言：「{src}」 -{type}-> 「{dst}」
该边类型的含义：{semantics}

材料：
- src「{src}」定义：{src_def}
- dst「{dst}」定义：{dst_def}
- 提取时给出的依据：{rationale}
- 来源正文节选：
---
{excerpt}
---

判断：材料是否支持这条断言？方向（src、dst 谁在前）是否正确？类型是否恰当？
输出 JSON：{{"verdict": "支持|不支持|证据不足", "direction_ok": true/false, "reason": "一句话理由，若类型或方向不对给出建议"}}"""

NODE_JUDGE_PROMPT = """你是教学知识图谱的独立复核员。只依据下面给出的材料判断，禁止使用你自己的记忆知识；材料不足就答「证据不足」。

图谱的概念粒度规则：只有「教科书会单独设章节、其他知识点会单独依赖」的知识点才立独立节点；
算法的内部步骤、阶段、参数、公式细节应作为某个已有概念的 facet（≤12 字侧面标签），不立节点。

待复核的候选节点：「{name}」
- 定义：{definition}
- 来源正文节选：
---
{excerpt}
---
- 图谱中与它相邻的已有概念：{neighbors}

判断：(1) 材料是否支持该定义；(2) 按粒度规则它应该是独立概念，还是某个邻近概念的 facet？
输出 JSON：{{"verdict": "独立概念|应为facet|证据不足", "facet_of": "若应为facet，属于哪个概念", "reason": "一句话理由"}}"""

EXCERPT_CHARS = 500  # evidence 前后各取这么多字作复核上下文


def _page_links_lower(page: dict) -> set:
    return {t.lower() for t in page["links"]}


def _links_to(links_lower: set, other_node: dict, other_page: dict | None) -> bool:
    """页面 A 的内链是否指向节点 B（按 B 的页面标题/重定向及名字/别名匹配）。"""
    targets = {other_node["name"].lower(), *(a.lower() for a in other_node["aliases"])}
    if other_page:
        targets |= {other_page["title"].lower(), *(r.lower() for r in other_page["redirects"])}
    return bool(links_lower & targets)


def structural_signals(conn) -> list[str]:
    """给所有 proposed 边/节点算结构佐证（零 LLM），写入 review_signals。返回日志行。"""
    lines = []
    index = corpus.title_index(conn)
    nodes = {n["id"]: n for n in db.list_nodes(conn)}
    nq = wikidata.node_qids(conn)
    page_cache, links_cache = {}, {}

    def page_of(nid):
        if nid not in page_cache:
            page_cache[nid] = corpus.page_for_node(conn, nodes[nid], index, with_text=False)
            if page_cache[nid]:
                links_cache[nid] = _page_links_lower(page_cache[nid])
        return page_cache[nid]

    n_edges = 0
    for e in db.list_edges(conn, status="proposed"):
        pa, pb = page_of(e["src"]), page_of(e["dst"])
        sig = {}
        if pa and pb:
            sig["link_src_dst"] = _links_to(links_cache[e["src"]], nodes[e["dst"]], pb)
            sig["link_dst_src"] = _links_to(links_cache[e["dst"]], nodes[e["src"]], pa)
            if e["type"] == "prerequisite_of":
                # RefD 直觉：讲解 dst 的页面引用 src => 支持 src 先修 dst；只反向引用则方向可疑
                if sig["link_dst_src"] and not sig["link_src_dst"]:
                    sig["refd"] = 1
                elif sig["link_src_dst"] and not sig["link_dst_src"]:
                    sig["refd"] = -1
                else:
                    sig["refd"] = 0
        qa, qb = nq.get(e["src"]), nq.get(e["dst"])
        if qa and qb:
            rels = wikidata.relation_between(conn, qa, qb)
            sig["wikidata"] = "，".join(r.replace("a→b", "src→dst").replace("b→a", "dst→src")
                                        for r in rels) or None
        if sig:
            db.save_signals(conn, "edge", e["id"], signals=sig)
            n_edges += 1

    n_nodes = 0
    for n in db.list_nodes(conn, status="proposed"):
        page = page_of(n["id"])
        sig = {"has_page": bool(page)}
        if page:
            titles = {page["title"].lower(), *(r.lower() for r in page["redirects"])}
            sig["exact_title"] = n["name"].lower() in titles or \
                any(a.lower() in titles for a in n["aliases"])
        db.save_signals(conn, "node", n["id"], signals=sig)
        n_nodes += 1
    conn.commit()
    lines.append(f"结构佐证：边 {n_edges} 条，节点 {n_nodes} 个")
    return lines


def _strip_prefixes(rationale: str) -> str:
    return re.sub(r"^(\[[^\]]{1,12}\]\s*)+", "", rationale)


def _source_excerpt(conn, source: str, evidence: str) -> str | None:
    """按 source（wiki:lang:title@rev）取来源页，截 evidence 附近的上下文。"""
    m = re.match(r"wiki:(zh|en):(.+)@\d+$", source)
    if not m:
        return None
    page = corpus.get_page(conn, m.group(1), m.group(2))
    if not page:
        return None
    text = page["text"]
    probe = _strip_prefixes(evidence).strip()
    pos = text.find(probe[:20]) if probe else -1
    if pos < 0:
        return text[:EXCERPT_CHARS * 2]
    return text[max(0, pos - EXCERPT_CHARS): pos + len(probe) + EXCERPT_CHARS]


def _node_head(conn, node: dict, index, chars=400) -> str:
    page = corpus.page_for_node(conn, node, index)
    return page["text"][:chars] if page else "（无语料页）"


def llm_review(conn, limit=10, redo=False) -> list[str]:
    """LLM 判断题复核，节点优先（节点裁决影响边），结果写 review_signals。"""
    lines = []
    index = corpus.title_index(conn)
    nodes = {n["id"]: n for n in db.list_nodes(conn)}
    names = {i: n["name"] for i, n in nodes.items()}
    done = 0

    def judged(item_type, item_id):
        sig = db.get_signals(conn, item_type, item_id)
        return bool(sig and sig.get("llm_verdict"))

    for n in db.list_nodes(conn, status="proposed"):
        if done >= limit:
            break
        if not redo and judged("node", n["id"]):
            continue
        neighbors = sorted({names[e["src"] if e["dst"] == n["id"] else e["dst"]]
                            for e in db.list_edges(conn)
                            if n["id"] in (e["src"], e["dst"])} - {n["name"]})
        excerpt = _source_excerpt(conn, n["source"], n["definition"]) \
            or _node_head(conn, n, index, EXCERPT_CHARS * 2)
        try:
            ans = llm.chat_json([{"role": "user", "content": NODE_JUDGE_PROMPT.format(
                name=n["name"], definition=n["definition"], excerpt=excerpt,
                neighbors="、".join(neighbors) or "无")}])
        except (RuntimeError, ValueError) as exc:
            lines.append(f"节点「{n['name']}」复核失败（{exc}），跳过")
            continue
        verdict = str(ans.get("verdict", "证据不足"))
        if verdict == "应为facet" and ans.get("facet_of"):
            verdict = f"应为facet→{ans['facet_of']}"
        db.save_signals(conn, "node", n["id"], llm_verdict=verdict,
                        llm_reason=str(ans.get("reason", ""))[:120])
        conn.commit()  # 逐条落库：批次中途失败不丢已复核的
        lines.append(f"节点「{n['name']}」: {verdict}（{ans.get('reason', '')}）")
        done += 1

    visible = set(db.visible_statuses())
    for e in db.list_edges(conn, status="proposed"):
        if done >= limit:
            break
        if nodes[e["src"]]["status"] not in visible or nodes[e["dst"]]["status"] not in visible:
            continue  # 端点未生效的边等节点裁决后再复核
        if not redo and judged("edge", e["id"]):
            continue
        excerpt = _source_excerpt(conn, e["source"], e["rationale"])
        if excerpt is None:  # 结构挖掘边没有正文证据，给两端页面开头
            excerpt = (f"（该边来自结构挖掘，无正文证据，以下是两端概念的来源页开头）\n"
                       f"《{names[e['src']]}》: {_node_head(conn, nodes[e['src']], index)}\n"
                       f"《{names[e['dst']]}》: {_node_head(conn, nodes[e['dst']], index)}")
        try:
            ans = llm.chat_json([{"role": "user", "content": EDGE_JUDGE_PROMPT.format(
                src=names[e["src"]], dst=names[e["dst"]], type=e["type"],
                semantics=EDGE_TYPE_SEMANTICS[e["type"]],
                src_def=nodes[e["src"]]["definition"] or "（无）",
                dst_def=nodes[e["dst"]]["definition"] or "（无）",
                rationale=e["rationale"] or "（无）", excerpt=excerpt)}])
        except (RuntimeError, ValueError) as exc:
            lines.append(f"边 {e['id']} 复核失败（{exc}），跳过")
            continue
        verdict = str(ans.get("verdict", "证据不足"))
        if not ans.get("direction_ok", True):
            verdict += "(方向存疑)"
        db.save_signals(conn, "edge", e["id"], llm_verdict=verdict,
                        llm_reason=str(ans.get("reason", ""))[:120])
        conn.commit()
        lines.append(f"边 {names[e['src']]} -{e['type']}-> {names[e['dst']]}: "
                     f"{verdict}（{ans.get('reason', '')}）")
        done += 1
    lines.append(f"LLM 复核 {done} 条")
    return lines


def apply_auto(conn) -> list[str]:
    """双重一致自动裁决（仅边）：结构佐证与 LLM 复核这两个独立信源意见一致才动。"""
    lines = []
    nodes = {n["id"]: n for n in db.list_nodes(conn)}
    names = {i: n["name"] for i, n in nodes.items()}
    visible = set(db.visible_statuses())
    n_approve = n_reject = 0
    for e in db.list_edges(conn, status="proposed"):
        if nodes[e["src"]]["status"] not in visible or nodes[e["dst"]]["status"] not in visible:
            continue
        sig = db.get_signals(conn, "edge", e["id"])
        if not sig or not sig.get("llm_verdict"):
            continue
        s = sig["signals"]
        supported = bool(s.get("wikidata") or s.get("link_src_dst") or s.get("link_dst_src"))
        refd_against = e["type"] == "prerequisite_of" and s.get("refd", 0) < 0
        if sig["llm_verdict"] == "支持" and supported and not refd_against:
            conn.execute("UPDATE edges SET status='approved' WHERE id=?", (e["id"],))
            db.log_review(conn, "edge", e["id"], "approve",
                          detail=sig.get("llm_reason") or "", source=e["source"], decided_by="auto")
            n_approve += 1
            lines.append(f"自动批准: {names[e['src']]} -{e['type']}-> {names[e['dst']]}")
        elif sig["llm_verdict"].startswith("不支持") and not supported:
            conn.execute("UPDATE edges SET status='rejected' WHERE id=?", (e["id"],))
            db.log_review(conn, "edge", e["id"], "reject",
                          detail=sig.get("llm_reason") or "", source=e["source"], decided_by="auto")
            n_reject += 1
            lines.append(f"自动拒绝: {names[e['src']]} -{e['type']}-> {names[e['dst']]}"
                         f"（{sig.get('llm_reason') or ''}）")
    conn.commit()
    lines.append(f"自动裁决：批准 {n_approve}，拒绝 {n_reject}（其余留人工；记得 kg review --audit 抽检）")
    return lines
