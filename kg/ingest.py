"""来源提取：语料库页面 -> M3 有据提取 -> 去重 -> proposed 入库。

两种模式：
- 锚点模式（ingest_topic）：围绕图谱已有节点，从它的页面提取邻居 + 补充 anchor_facets。
- 主题模式（ingest_hypothesis）：expand 假设生成器验证通过的候选新概念，
  从它自己的页面提取该概念本身及其与已有节点的有据边。
"""
import math
import re
import time

from . import corpus, db, dedup, docs, llm

EXTRACT_PROMPT = """你在为一个 AI 领域的教学知识图谱从权威来源提取知识。下面是{origin}的正文节选。

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
5. 误区提取：若正文**明确指出**某概念的常见错误理解或易混淆点（如「常被误认为」
   「注意不要混淆」「一个常见的错误是」），输出到 misconceptions：
   text 是 ≤40 字的误区陈述（描述错误认识本身，如「更深的网络一定效果更好」），
   evidence 同样必须逐字摘录；正文没有明说的误区不要凭记忆编造，没有就输出空数组。
6. {focus}

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
"anchor_facets": ["{anchor_facets_hint}"],
"misconceptions": [
  {{"concept": "该误区属于哪个概念（图谱已有节点名或本次提取的概念名）",
    "text": "≤40字的误区陈述",
    "evidence": "正文原文摘录"}}
]}}"""

ANCHOR_FOCUS = ("中心主题「{anchor}」在图谱中已存在，围绕它提取邻居概念；"
                "提取 1~{limit} 个最重要的概念即可，宁缺毋滥。")
TOPIC_FOCUS = ("「{topic}」是一个候选新概念（图谱中尚不存在），本页是它的来源页。"
               "请把「{topic}」本身作为第一个概念输出（定义、facets、证据），"
               "并给出它与图谱已有节点之间有据可查的边；"
               "若正文还明确讨论其他重要概念，可再提取至多 {extra} 个。")

REQUOTE_PROMPT = """你此前从下面的正文中提取了一个断言，但给出的 evidence 与正文原文不符（可能被改写过）。
请重新从正文中**逐字摘录**一句能支持该断言的原文（≤60字，可用省略号截断中间部分）；
若正文中找不到能支持它的原句，输出空字符串。

断言：{claim}
此前给出的 evidence（与原文不符）：{bad}

正文：
---
{text}
---

输出 JSON：{{"evidence": "逐字摘录或空字符串"}}"""

INFERRED_MAX_CONFIDENCE = 0.6
MISCONCEPTION_MAX_CHARS = 40  # 误区陈述长度上限（普通 facet 是 ≤12 字名词短语，误区是短句）

CHUNK_CHARS = 6000  # 单块送 LLM 的正文上限（分块取代旧的整页截断）
MAX_BLOCKS = 3      # 每页最多提取的块数（成本保险丝）
SKIP_SECTIONS = {"参考文献", "参考资料", "外部链接", "参见", "延伸阅读", "注释", "脚注",
                 "references", "external links", "see also", "further reading",
                 "notes", "bibliography", "sources"}

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
    focus_fn = lambda k: ANCHOR_FOCUS.format(anchor=anchor["name"], limit=k)
    hint = f"正文中提到的、应补充为「{anchor['name']}」facets 的细节侧面，可为空"
    return _extract(conn, page, focus_fn, limit, anchor=anchor, hint=hint, dry_run=dry_run)


def ingest_hypothesis(conn, topic: str, page: dict, extra=1, dry_run=False) -> dict:
    """主题模式：候选新概念的来源页 -> 提取该概念及其与已有节点的边。"""
    focus_fn = lambda k: TOPIC_FOCUS.format(topic=topic, extra=extra)  # extra 本身很小，不平摊
    return _extract(conn, page, focus_fn, extra + 1, anchor=None,
                    hint="输出空数组", dry_run=dry_run)


def ingest_topic_doc(conn, term: str, book=None, limit=6, dry_run=False) -> dict:
    """锚点模式（教材通道）：从文档语料的对应章节提取，约束与 wiki 通道完全一致。
    英文源在此处触发 lazy 翻译；evidence 对翻译后的中文正文校验。"""
    anchor = db.find_by_name_or_alias(conn, term)
    if not anchor:
        raise KeyError(f"锚点节点不存在: {term}（新知识需要挂靠已有骨架）")
    sec = docs.section_for_node(conn, anchor, book)
    if not sec:
        return {"error": f"文档语料里找不到「{anchor['name']}」的章节"
                         f"（book={book or '全部'}；先 kg docs fetch）"}
    sec = docs.ensure_text(conn, sec)
    cfg_title = docs.load_book(sec["book"])["title"]
    page = {"title": sec["title"], "text": sec["text"]}
    focus_fn = lambda k: ANCHOR_FOCUS.format(anchor=anchor["name"], limit=k)
    hint = f"正文中提到的、应补充为「{anchor['name']}」facets 的细节侧面，可为空"
    return _extract(conn, page, focus_fn, limit, anchor=anchor, hint=hint, dry_run=dry_run,
                    source=docs.source_of(sec), url=docs.url_of(sec),
                    origin=f"教材《{cfg_title}》第 {sec['sec_id']} 节「{sec['title']}」")


def split_blocks(text: str) -> list[str]:
    """把正文切成 ≤CHUNK_CHARS 的块：优先按维基 extract 的 == 节标题 == 切节
    （跳过参考文献类节），超长节按空行段落再切，相邻小块贪心合并。
    无节标题的文本（教材节）退化为纯段落聚合。"""
    parts = re.split(r"(?m)^\s*(={2,}\s*.+?\s*={2,})\s*$", text)
    units = []
    lead = parts[0].strip()
    if lead:
        units.append(lead)
    for i in range(1, len(parts), 2):
        header = parts[i].strip("= \t")
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if header.lower() in SKIP_SECTIONS or not body:
            continue
        units.append(f"{header}\n{body}")
    blocks, cur = [], ""
    for unit in units:
        for piece in (docs.split_chunks(unit, CHUNK_CHARS) if len(unit) > CHUNK_CHARS
                      else [unit]):
            if cur and len(cur) + len(piece) + 1 > CHUNK_CHARS:
                blocks.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n{piece}" if cur else piece
    if cur:
        blocks.append(cur)
    return blocks


def pick_blocks(blocks: list[str], focus_names: list[str]) -> list[str]:
    """选送 LLM 的块：首块（导言）必选，其余按焦点名出现密度取满 MAX_BLOCKS，保持原文顺序。"""
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    keys = [_norm(n) for n in focus_names if n]
    scored = sorted(range(1, len(blocks)),
                    key=lambda i: (-sum(_norm(blocks[i]).count(k) for k in keys), i))
    chosen = sorted([0] + scored[:MAX_BLOCKS - 1])
    return [blocks[i] for i in chosen]


def _requote_failed(page, blocks, concepts, misconceptions, stats):
    """evidence 子串校验失败的条目丢弃前给一次「逐字重引」机会（Self-Refine 轻量版）：
    概念/边/误区往往是对的，只是引文被改写。重引后仍须通过同一校验，不变式不破坏。
    只重试一轮，不做多轮修正循环。"""
    corpus_text = "\n".join(blocks)  # LLM 实际看到的正文（是整页的子串）
    retries = []
    for c in concepts:
        if c.get("name") and not evidence_in_text(c.get("evidence", ""), page["text"]):
            retries.append((c, f"概念「{c['name']}」：{c.get('definition', '')}"))
        for e in c.get("edges", []):
            if not evidence_in_text(e.get("evidence", ""), page["text"]):
                retries.append((e, f"「{c.get('name', '')}」-{e.get('type', '?')}->"
                                   f"「{e.get('other', '')}」"))
    for m in misconceptions:
        if m.get("text") and not evidence_in_text(m.get("evidence", ""), page["text"]):
            retries.append((m, f"「{m.get('concept', '')}」的常见误区：{m['text']}"))
    if not retries:
        return

    def call(prompt):
        try:
            return llm.chat_json([{"role": "user", "content": prompt}])
        except (RuntimeError, ValueError):
            return {}

    prompts = [REQUOTE_PROMPT.format(claim=claim, bad=item.get("evidence", ""),
                                     text=corpus_text) for item, claim in retries]
    for (item, claim), ans in zip(retries, llm.pmap(call, prompts)):
        ev = str(ans.get("evidence") or "").strip()
        if ev and evidence_in_text(ev, page["text"]):
            item["evidence"] = ev
            stats["requoted"] += 1
            stats["details"].append(f"重引成功: {claim[:40]}")


def _extract(conn, page, focus_fn, cap, anchor=None, hint="", dry_run=False,
             source=None, url=None, origin=None) -> dict:
    """从一个来源页/教材节提取。wiki 通道用默认 source/url/origin，doc 通道显式传入。

    cap 是**每页**的概念产出预算：平摊到各块（focus_fn(每块配额) 生成焦点指令），
    合并后超出预算的截断丢弃——分块不放大"宁缺毋滥"的总闸门。"""
    source = source or corpus.source_of(page)
    url = url or corpus.url_of(page)
    origin = origin or f"维基百科页面《{page['title']}》"
    all_names = [n["name"] for n in db.list_nodes(conn) if n["status"] != "rejected"]
    focus_names = ([anchor["name"]] + anchor["aliases"]) if anchor else [page["title"]]
    blocks = pick_blocks(split_blocks(page["text"]), focus_names)
    focus = focus_fn(max(1, math.ceil(cap / len(blocks))))

    stats = {"page": url, "source": source, "blocks": len(blocks),
             "proposed_nodes": 0, "merged_aliases": 0, "demoted_facets": 0,
             "proposed_edges": 0, "anchor_facets_added": 0, "dropped_related": 0,
             "dropped_no_evidence": 0, "requoted": 0, "misconceptions": 0, "details": []}

    # 全部 LLM 调用先完成（块间并行，fn 内不碰 conn）再入库：中途失败不留半页产物
    prompts = [EXTRACT_PROMPT.format(origin=origin, focus=focus, anchor_facets_hint=hint,
                                     all_names="、".join(all_names), text=block)
               for block in blocks]
    raw_results = llm.pmap(
        lambda p: llm.chat_json([{"role": "user", "content": p}]), prompts)
    concepts, anchor_facets, misconceptions, seen, mis_seen = [], [], [], set(), set()
    for result in raw_results:
        anchor_facets += result.get("anchor_facets", [])
        for c in result.get("concepts", []):
            nm = c.get("name", "").strip()
            if nm and nm.lower() not in seen:  # 同一概念在多块出现只取首次
                seen.add(nm.lower())
                concepts.append(c)
        for m in result.get("misconceptions", []):
            key = (str(m.get("concept", "")).strip(), str(m.get("text", "")).strip())
            if all(key) and key not in mis_seen:
                mis_seen.add(key)
                misconceptions.append(m)
    if dry_run:
        stats["details"] = raw_results if len(raw_results) > 1 else raw_results[0]
        return stats
    if len(concepts) > cap:
        stats["details"].append(f"超出每页预算 {cap}，丢弃后 {len(concepts) - cap} 个概念")
        concepts = concepts[:cap]
    _requote_failed(page, blocks, concepts, misconceptions, stats)

    # 锚点 facets 补充（直接生效：有正文依据且不改拓扑）；长句不是 facet，过滤掉
    if anchor:
        new_facets = [f for f in dict.fromkeys(anchor_facets)
                      if f and len(f) <= 16 and f not in anchor["facets"]]
        if new_facets:
            db.update_node(conn, anchor["id"], facets=anchor["facets"] + new_facets)
            stats["anchor_facets_added"] = len(new_facets)
            stats["details"].append(f"锚点「{anchor['name']}」补充 facets: {', '.join(new_facets)}")

    name_to_id = {}
    for c in concepts:
        name, definition = c.get("name", "").strip(), c.get("definition", "")
        if not name:
            continue
        if not evidence_in_text(c.get("evidence", ""), page["text"]):
            stats["dropped_no_evidence"] += 1
            stats["details"].append(f"丢弃「{name}」：evidence 不是正文原文（{c.get('evidence', '')[:40]}）")
            continue
        existing, how = dedup.find_duplicate(conn, name, definition)
        if existing and how.startswith("facet:"):
            # 粒度裁决：相似但更细粒度，降级为已有节点的 facet；其边不入库
            if name not in existing["facets"]:
                db.update_node(conn, existing["id"], facets=existing["facets"] + [name])
            stats["demoted_facets"] += 1
            stats["details"].append(f"「{name}」按粒度裁决降级为「{existing['name']}」"
                                    f"的 facet（{how}），其边一并丢弃")
            continue
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

    for c in concepts:
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

    # 误区：带前缀的特殊 facet，挂到归属概念上（本次概念或图谱已有节点，含锚点）。
    # 与 anchor_facets 同理直接生效——有正文依据且不改拓扑；归属概念找不到就丢弃。
    for m in misconceptions:
        cname, text = str(m.get("concept", "")).strip(), str(m.get("text", "")).strip()
        if not cname or not text or len(text) > MISCONCEPTION_MAX_CHARS:
            continue
        if not evidence_in_text(m.get("evidence", ""), page["text"]):
            stats["dropped_no_evidence"] += 1
            stats["details"].append(f"丢弃误区「{text[:30]}」：evidence 不是正文原文")
            continue
        target = (db.get_node(conn, name_to_id[cname]) if cname in name_to_id
                  else db.find_by_name_or_alias(conn, cname))
        if not target or target["status"] == "rejected":
            continue
        facet = db.MISCONCEPTION_PREFIX + text
        if facet not in target["facets"]:
            db.update_node(conn, target["id"], facets=target["facets"] + [facet])
            stats["misconceptions"] += 1
            stats["details"].append(f"「{target['name']}」记录误区: {text}")

    log_name = anchor["name"] if anchor else f"topic:{page['title']}"
    conn.execute("INSERT OR IGNORE INTO ingest_log(anchor, source, created_at) VALUES (?,?,?)",
                 (log_name, source, time.time()))
    conn.commit()
    return stats


def pick_anchors_doc(conn, k=3, book=None) -> list[dict]:
    """教材通道的缺口驱动选锚点（语料分级：教材是 high 级语料，批量提取优先走这里）。

    「教材单独设节讲解（标题命中，权重 2）或正文提及（权重 1）、而图谱稀疏」的
    生效节点优先：score = 命中权重 / (1 + 生效度数)。
    跳过同一节版本已 ingest 过的锚点（未翻译节 hash 未定，按节前缀查重）。"""
    done = [(r["anchor"], r["source"])
            for r in conn.execute("SELECT anchor, source FROM ingest_log")]
    done_set = set(done)
    scored = []
    for node in db.list_nodes(conn):
        if node["status"] not in db.visible_statuses():
            continue
        sec, how = docs.section_for_node(conn, node, book, with_how=True)
        if not sec:
            continue
        if sec["content_hash"]:
            if (node["name"], docs.source_of(sec)) in done_set:
                continue
        else:
            prefix = f"doc:{sec['book']}:{sec['sec_id']}@"
            if any(a == node["name"] and s.startswith(prefix) for a, s in done):
                continue
        weight = 2.0 if how == "title" else 1.0
        score = weight / (1 + db.degree(conn, node["id"]))
        scored.append((score, sec, how, node))
    scored.sort(key=lambda x: -x[0])
    return [{"node": n, "score": s, "section": sec, "how": how}
            for s, sec, how, n in scored[:k]]


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
