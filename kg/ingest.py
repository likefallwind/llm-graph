"""来源提取：语料库页面 -> M3 有据提取 -> 去重 -> proposed 入库。

两种模式：
- 锚点模式（ingest_topic）：围绕图谱已有节点，从它的页面提取邻居 + 补充 anchor_facets。
- 主题模式（ingest_hypothesis）：expand 假设生成器验证通过的候选新概念，
  从它自己的页面提取该概念本身及其与已有节点的有据边。
"""
import time

from . import corpus, db, dedup, llm, wiki

EXTRACT_PROMPT = """你在为一个 AI 领域的教学知识图谱从权威来源提取知识。下面是维基百科页面《{title}》的正文节选。

严格规则：
1. 只提取正文中**明确讨论**的概念和关系，禁止依赖你自己的记忆补充正文没有的内容。
2. 每个概念、每条边都必须给出 evidence：从正文中摘出的一句原文（可截断，≤60字）。
3. 概念粒度（最重要的规则）：
   - 只有「教科书会单独设章节、其他知识点会单独依赖」的知识点才能作为概念输出。
   - 算法的内部步骤、阶段、参数、公式细节一律不是概念，放进 facets 或 anchor_facets。
   - facet 必须是 ≤12 字的名词短语（如「学习率」「遗忘门」），不是句子。
   - 拿不准时宁可放 facets，不要立概念。
4. 边类型（4 种）：
   - is_a：A 是 B 的一种
   - part_of：A 是 B 的组成部分
   - prerequisite_of：不懂 A 就无法理解 B（教学先修，最有价值）。
     这是唯一允许推断的边：正文没有明说先修关系时，若 A、B 都在正文中出现且教学依赖明确，
     可以推断此边，但必须加 "inferred": true，confidence 不高于 0.6，
     evidence 给出 A、B 同时出现的原文。
   - related_to：受严格限制，仅允许以下三种情形之一，且必须在该边的 kind 字段注明：
     * 同题替代：A 和 B 是解决同一问题的两种方法（如 GRU 与 LSTM）
     * 演化启发：正文明确说 A 直接启发或发展出 B
     * 教学对比：教材中常把 A 和 B 放在一起对比讲解（如 L1 与 L2 正则化）
     「同属一个领域」「正文顺带提到」不算相关；不属于这三种就不要连 related_to。
5. {focus}

图谱中已有的节点名（提取到同义词时仍然照常输出，系统会自动合并；但不要输出与这些完全相同的名字）：
{all_names}

正文节选：
---
{text}
---

输出 JSON：
{{"concepts": [
  {{"name": "概念名（中文优先，英文术语放 aliases）",
    "aliases": ["..."],
    "definition": "依据正文写的一句话定义",
    "facets": ["..."],
    "evidence": "正文原文摘录",
    "edges": [
      {{"other": "已有节点名或本次提取的概念名",
        "type": "is_a|part_of|prerequisite_of|related_to",
        "kind": "仅 related_to 必填：同题替代|演化启发|教学对比",
        "inferred": "仅 prerequisite_of 推断边时为 true",
        "direction": "new_to_other 或 other_to_new",
        "confidence": 0.0~1.0,
        "evidence": "支持这条边的正文摘录"}}
    ]}}
],
"anchor_facets": ["{anchor_facets_hint}"]}}"""

ANCHOR_FOCUS = ("中心主题「{anchor}」在图谱中已存在，围绕它提取邻居概念；"
                "提取 1~{limit} 个最重要的概念即可，宁缺毋滥。")
TOPIC_FOCUS = ("「{topic}」是一个候选新概念（图谱中尚不存在），本页是它的来源页。"
               "请把「{topic}」本身作为第一个概念输出（定义、facets、证据），"
               "并给出它与图谱已有节点之间有据可查的边；"
               "若正文还明确讨论其他重要概念，可再提取至多 {extra} 个。")

INFERRED_MAX_CONFIDENCE = 0.6

_ELLIPSIS = ("…", "...", "。。。")


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def evidence_in_text(evidence: str, text: str) -> bool:
    """evidence 必须真的出自正文（防 LLM 编造"原文"）：
    规范化（去空白、小写）后做子串校验；摘录可含省略号截断，按省略号切段逐段校验。"""
    if not evidence or not evidence.strip():
        return False
    norm_text = _norm(text)
    parts = [evidence]
    for e in _ELLIPSIS:
        parts = [q for p in parts for q in p.split(e)]
    parts = [_norm(p) for p in parts if _norm(p)]
    return bool(parts) and all(p in norm_text for p in parts)


def ingest_topic(conn, term: str, limit=6, dry_run=False) -> dict:
    """锚点模式：围绕图谱已有节点，从语料库（缺页自动抓取）提取邻居知识。"""
    anchor = db.find_by_name_or_alias(conn, term)
    if not anchor:
        raise KeyError(f"锚点节点不存在: {term}（新知识需要挂靠已有骨架）")
    page = corpus.page_for_node(conn, anchor)
    if not page:
        for q in [anchor["name"]] + anchor["aliases"]:
            page = corpus.fetch_and_store(conn, q)
            if page:
                corpus.map_node(conn, anchor["id"], page)
                break
    if not page:
        return {"error": f"语料库和 Wikipedia 上都找不到「{anchor['name']}」的有效页面"}
    focus = ANCHOR_FOCUS.format(anchor=anchor["name"], limit=limit)
    hint = f"正文中提到的、应补充为「{anchor['name']}」facets 的细节侧面，可为空"
    return _extract(conn, page, focus, anchor=anchor, hint=hint, dry_run=dry_run)


def ingest_hypothesis(conn, topic: str, page: dict, extra=1, dry_run=False) -> dict:
    """主题模式：候选新概念的来源页 -> 提取该概念及其与已有节点的边。"""
    focus = TOPIC_FOCUS.format(topic=topic, extra=extra)
    return _extract(conn, page, focus, anchor=None, hint="输出空数组", dry_run=dry_run)


def _extract(conn, page, focus, anchor=None, hint="", dry_run=False) -> dict:
    all_names = [n["name"] for n in db.list_nodes(conn) if n["status"] != "rejected"]
    result = llm.chat_json([{"role": "user", "content": EXTRACT_PROMPT.format(
        title=page["title"], focus=focus, anchor_facets_hint=hint,
        all_names="、".join(all_names),
        text=page["text"][:wiki.MAX_TEXT_CHARS])}])

    source = corpus.source_of(page)
    stats = {"page": corpus.url_of(page), "source": source,
             "proposed_nodes": 0, "merged_aliases": 0, "proposed_edges": 0,
             "anchor_facets_added": 0, "dropped_related": 0,
             "dropped_no_evidence": 0, "details": []}
    if dry_run:
        stats["details"] = result
        return stats

    # 锚点 facets 补充（直接生效：有正文依据且不改拓扑）；长句不是 facet，过滤掉
    if anchor:
        new_facets = [f for f in result.get("anchor_facets", [])
                      if f and len(f) <= 16 and f not in anchor["facets"]]
        if new_facets:
            db.update_node(conn, anchor["id"], facets=anchor["facets"] + new_facets)
            stats["anchor_facets_added"] = len(new_facets)
            stats["details"].append(f"锚点「{anchor['name']}」补充 facets: {', '.join(new_facets)}")

    name_to_id = {}
    for c in result.get("concepts", []):
        name, definition = c.get("name", "").strip(), c.get("definition", "")
        if not name:
            continue
        if not evidence_in_text(c.get("evidence", ""), page["text"]):
            stats["dropped_no_evidence"] += 1
            stats["details"].append(f"丢弃「{name}」：evidence 不是正文原文（{c.get('evidence', '')[:40]}）")
            continue
        existing, how = dedup.find_duplicate(conn, name, definition)
        if existing:
            dedup.merge_as_alias(conn, existing, name)
            name_to_id[name] = existing["id"]
            stats["merged_aliases"] += 1
            stats["details"].append(f"「{name}」已存在（{how}），合并为「{existing['name']}」的别名")
            continue
        node_id = db.add_node(conn, name, definition=definition,
                              aliases=c.get("aliases", []), facets=c.get("facets", []),
                              status="proposed", source=source, embedding=how)
        name_to_id[name] = node_id
        stats["proposed_nodes"] += 1
        stats["details"].append(f"新提议节点「{name}」  证据: {c.get('evidence', '')[:60]}")

    for c in result.get("concepts", []):
        new_id = name_to_id.get(c.get("name", "").strip())
        if new_id is None:
            continue
        for e in c.get("edges", []):
            other = db.find_by_name_or_alias(conn, e.get("other", ""))
            other_id = other["id"] if other else name_to_id.get(e.get("other", "").strip())
            if other_id is None or other_id == new_id or e.get("type") not in db.EDGE_TYPES:
                continue
            if not evidence_in_text(e.get("evidence", ""), page["text"]):
                stats["dropped_no_evidence"] += 1
                continue
            rationale = e.get("evidence", "")
            confidence = float(e.get("confidence", 0.5))
            if e["type"] == "related_to":
                if e.get("kind") not in db.RELATED_KINDS:
                    stats["dropped_related"] += 1
                    continue
                rationale = f"[{e['kind']}] {rationale}"
            if e["type"] == "prerequisite_of" and e.get("inferred") in (True, "true"):
                rationale = f"[推断] {rationale}"
                confidence = min(confidence, INFERRED_MAX_CONFIDENCE)
            if e.get("direction") == "other_to_new":
                src, dst = other_id, new_id
            else:
                src, dst = new_id, other_id
            rowid = db.add_edge(conn, src, dst, e["type"], confidence=confidence,
                                rationale=rationale, source=source, status="proposed")
            if rowid:
                stats["proposed_edges"] += 1

    log_name = anchor["name"] if anchor else f"topic:{page['title']}"
    conn.execute("INSERT OR IGNORE INTO ingest_log(anchor, source, created_at) VALUES (?,?,?)",
                 (log_name, source, time.time()))
    conn.commit()
    return stats


def pick_anchors(conn, k=3) -> list[dict]:
    """缺口驱动选锚点：语料内链入度高（重要）而图谱度数低（缺口）的生效节点优先。

    score = 内链入度 / (1 + 生效度数)；跳过同一页面版本已 ingest 过的。
    """
    index = corpus.title_index(conn)
    counts = corpus.link_counts(conn)
    done = {(r["anchor"], r["source"])
            for r in conn.execute("SELECT anchor, source FROM ingest_log")}
    scored = []
    for node in db.list_nodes(conn):
        if node["status"] not in db.visible_statuses():
            continue
        page = corpus.page_for_node(conn, node, index, with_text=False)
        if not page or (node["name"], corpus.source_of(page)) in done:
            continue
        indeg = sum(counts.get((page["lang"], t.lower()), 0)
                    for t in [page["title"]] + page["redirects"])
        score = indeg / (1 + db.degree(conn, node["id"]))
        scored.append((score, indeg, node))
    scored.sort(key=lambda x: -x[0])
    return [{"node": n, "score": s, "indegree": d} for s, d, n in scored[:k]]
