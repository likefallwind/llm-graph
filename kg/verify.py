"""复核层：给 proposed 条目积累独立佐证，支撑"人工审核 -> AI 为主 + 人工抽检"的过渡。

两级信号，写入 review_signals，review 时展示：
- 结构佐证（零 LLM）：语料页互链、RefD 式先修方向信号、Wikidata claim。
  这些是人类校对过的结构，与 LLM 提取相互独立。
- LLM 判断题复核：与提取不同的视角——只给材料（evidence、来源节选、双方定义），
  只回答是/否，禁止用模型记忆。复核时 LLM 也不是知识源，语料才是。

--apply 自动裁决规则（保守，两个独立信源一致才动）：
- 节点只自动批准：LLM 判「独立概念」+ 名字精确命中语料页标题/重定向
  + 语料页与图谱邻居有重叠（neighbor_overlap，UMAP 撞名补丁）；
  拒绝与降级 facet 一律留人工（降级要人选归属概念）。先裁节点再裁边。
- 边批准：LLM 判「支持」且方向无疑 + 至少一项结构佐证 + 先修方向信号不反对
  + 无环类型（先修/is_a/part_of）模拟加边不成环（守卫前移为放行拦截）
- 边拒绝：LLM 判「不支持」且无任何结构佐证
- low 级语料（quality.source_tier）的条目一律不自动裁决。
其余留人工。自动裁决记 review_log(decided_by='auto', batch_id=本次运行批次)，
用 kg review --audit 抽检；误放行的批次可 kg rollback <batch_id> 整批撤销。
"""
import re
import time
from collections import defaultdict

from . import corpus, db, docs, guards, llm, quality, wikidata

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


def _name_hit(name: str, raw_text: str, norm_text: str) -> bool:
    """名字是否出现在正文里：ASCII 名要求词边界（防 GRU 撞 congruent），中文规范化子串。"""
    if re.fullmatch(r"[\x00-\x7f]+", name):
        return bool(re.search(r"\b" + re.escape(name.lower()) + r"\b", raw_text.lower()))
    return "".join(name.split()).lower() in norm_text


def _neighbor_overlap(conn, node, nodes, edges, links_lower, index) -> bool:
    """UMAP 撞名补丁：节点的语料页是否提到它在图谱中的邻居概念。

    多义短名/缩写被重定向劫持到无关页面时「名字精确命中」天然成立，LLM 复核
    也只读得到错误页面的材料——两个信源被同一错误来源污染。错误页面不会提到
    真概念的图谱邻居，所以内容与邻域的重叠是恢复信源独立性的第三道检查。
    无邻居可查（孤立提议）时返回 False，不给自动放行开门。"""
    neigh_ids = {e["src"] if e["dst"] == node["id"] else e["dst"]
                 for e in edges if node["id"] in (e["src"], e["dst"])} - {node["id"]}
    names = []
    for nid in neigh_ids:
        nb = nodes.get(nid)
        if nb and nb["status"] != "rejected":
            names += [nb["name"]] + nb["aliases"]
    names = [nm for nm in names if len(nm) >= 3]
    if not names:
        return False
    if any(nm.lower() in links_lower for nm in names):
        return True
    page = corpus.page_for_node(conn, node, index)  # 带正文重取，只发生在 proposed 节点上
    if not page:
        return False
    norm_text = "".join(page["text"].split()).lower()
    return any(_name_hit(nm, page["text"], norm_text) for nm in names)


def _toc_vote(pa, pb):
    """教材目录序信号（仅先修边）：所有共同书里 src 首现章节序 < dst -> +1（支持方向），
    反之 -1；多书矛盾或同节 -> 0（宁可无信号不可假信号）；无共同书 -> None（不写键）。"""
    if not pa or not pb:
        return None
    votes = set()
    for book in pa.keys() & pb.keys():
        d = pb[book] - pa[book]
        votes.add(0 if d == 0 else (1 if d > 0 else -1))
    if not votes:
        return None
    return 1 if votes == {1} else (-1 if votes == {-1} else 0)


def structural_signals(conn) -> list[str]:
    """给所有 proposed 边/节点算结构佐证（零 LLM），写入 review_signals。返回日志行。"""
    lines = []
    index = corpus.title_index(conn)
    nodes = {n["id"]: n for n in db.list_nodes(conn)}
    nq = wikidata.node_qids(conn)
    toc_pos = docs.concept_positions(conn)  # {node_id: {book: 首现章节序}}，无文档语料时为空
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
        if e["type"] == "prerequisite_of":
            # 不依赖维基页面：纯教材来源的边也要有信号可写
            toc = _toc_vote(toc_pos.get(e["src"]), toc_pos.get(e["dst"]))
            if toc is not None:
                sig["toc"] = toc
        if sig:
            db.save_signals(conn, "edge", e["id"], signals=sig)
            n_edges += 1

    n_nodes = 0
    live_edges = [e for e in db.list_edges(conn) if e["status"] != "rejected"]
    for n in db.list_nodes(conn, status="proposed"):
        page = page_of(n["id"])
        sig = {"has_page": bool(page)}
        if page:
            titles = {page["title"].lower(), *(r.lower() for r in page["redirects"])}
            sig["exact_title"] = n["name"].lower() in titles or \
                any(a.lower() in titles for a in n["aliases"])
            sig["neighbor_overlap"] = _neighbor_overlap(
                conn, n, nodes, live_edges, links_cache[n["id"]], index)
        db.save_signals(conn, "node", n["id"], signals=sig)
        n_nodes += 1
    conn.commit()
    lines.append(f"结构佐证：边 {n_edges} 条，节点 {n_nodes} 个")
    return lines


def _strip_prefixes(rationale: str) -> str:
    return re.sub(r"^(\[[^\]]{1,12}\]\s*)+", "", rationale)


def _source_excerpt(conn, source: str, evidence: str) -> str | None:
    """按 source 取来源正文（wiki:lang:title@rev 或 doc:book:sec@hash），
    截 evidence 附近的上下文。"""
    text, note = None, ""
    m = re.match(r"wiki:(zh|en):(.+)@\d+$", source)
    if m:
        page = corpus.get_page(conn, m.group(1), m.group(2))
        if page:
            text = page["text"]
    else:
        d = docs.parse_source(source)
        if d:
            sec = docs.get_section(conn, d[0], d[1])
            if sec and sec["text"]:
                text = sec["text"]
                if sec["content_hash"] != d[2]:
                    note = "（注：该节文本在提取后已更新，节选取自当前版本）\n"
    if text is None:
        return None
    probe = _strip_prefixes(evidence).strip()
    pos = text.find(probe[:20]) if probe else -1
    if pos < 0:
        return note + text[:EXCERPT_CHARS * 2]
    return note + text[max(0, pos - EXCERPT_CHARS): pos + len(probe) + EXCERPT_CHARS]


def _node_head(conn, node: dict, index, chars=400) -> str:
    page = corpus.page_for_node(conn, node, index)
    return page["text"][:chars] if page else "（无语料页）"


def _judge_batch(prompts: list[str]) -> list:
    """并行调 M3 判断题（llm.pmap，全局并发上限内）；单项失败返回异常对象不杀整批。"""
    def call(p):
        try:
            return llm.chat_json([{"role": "user", "content": p}])
        except (RuntimeError, ValueError) as exc:
            return exc
    return llm.pmap(call, prompts)


def llm_review(conn, limit=10, redo=False) -> list[str]:
    """LLM 判断题复核，节点优先（节点裁决影响边），结果写 review_signals。
    prompt 准备（读库）与结果落库串行，LLM 调用批量并行。"""
    lines = []
    index = corpus.title_index(conn)
    nodes = {n["id"]: n for n in db.list_nodes(conn)}
    names = {i: n["name"] for i, n in nodes.items()}
    done = 0

    def judged(item_type, item_id):
        sig = db.get_signals(conn, item_type, item_id)
        return bool(sig and sig.get("llm_verdict"))

    node_batch = []  # (节点, prompt)
    for n in db.list_nodes(conn, status="proposed"):
        if len(node_batch) >= limit:
            break
        if not redo and judged("node", n["id"]):
            continue
        neighbors = sorted({names[e["src"] if e["dst"] == n["id"] else e["dst"]]
                            for e in db.list_edges(conn)
                            if n["id"] in (e["src"], e["dst"])} - {n["name"]})
        excerpt = _source_excerpt(conn, n["source"], n["definition"]) \
            or _node_head(conn, n, index, EXCERPT_CHARS * 2)
        node_batch.append((n, NODE_JUDGE_PROMPT.format(
            name=n["name"], definition=n["definition"], excerpt=excerpt,
            neighbors="、".join(neighbors) or "无")))
    for (n, _), ans in zip(node_batch, _judge_batch([p for _, p in node_batch])):
        if isinstance(ans, Exception):
            lines.append(f"节点「{n['name']}」复核失败（{ans}），跳过")
            continue
        verdict = str(ans.get("verdict", "证据不足"))
        if verdict == "应为facet" and ans.get("facet_of"):
            verdict = f"应为facet→{ans['facet_of']}"
        db.save_signals(conn, "node", n["id"], llm_verdict=verdict,
                        llm_reason=str(ans.get("reason", ""))[:120])
        lines.append(f"节点「{n['name']}」: {verdict}（{ans.get('reason', '')}）")
        done += 1
    conn.commit()

    visible = set(db.visible_statuses())
    edge_batch = []  # (边, prompt)
    for e in db.list_edges(conn, status="proposed"):
        if len(node_batch) + len(edge_batch) >= limit:
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
        edge_batch.append((e, EDGE_JUDGE_PROMPT.format(
            src=names[e["src"]], dst=names[e["dst"]], type=e["type"],
            semantics=EDGE_TYPE_SEMANTICS[e["type"]],
            src_def=nodes[e["src"]]["definition"] or "（无）",
            dst_def=nodes[e["dst"]]["definition"] or "（无）",
            rationale=e["rationale"] or "（无）", excerpt=excerpt)))
    for (e, _), ans in zip(edge_batch, _judge_batch([p for _, p in edge_batch])):
        if isinstance(ans, Exception):
            lines.append(f"边 {e['id']} 复核失败（{ans}），跳过")
            continue
        verdict = str(ans.get("verdict", "证据不足"))
        if not ans.get("direction_ok", True):
            verdict += "(方向存疑)"
        db.save_signals(conn, "edge", e["id"], llm_verdict=verdict,
                        llm_reason=str(ans.get("reason", ""))[:120])
        lines.append(f"边 {names[e['src']]} -{e['type']}-> {names[e['dst']]}: "
                     f"{verdict}（{ans.get('reason', '')}）")
        done += 1
    conn.commit()
    lines.append(f"LLM 复核 {done} 条")
    return lines


def apply_auto(conn) -> list[str]:
    """双重一致自动裁决：结构佐证与 LLM 复核这两个独立信源意见一致才动。

    先裁节点（只批准不拒绝），节点生效后其关联边才能进入边裁决；
    刚生效节点的边通常还没有 LLM 复核结论，下一轮 verify 会补上。
    三道新闸：low 级语料不自动裁决；节点另需 neighbor_overlap（UMAP 补丁）；
    无环类型边先模拟加边查环。整次运行共用一个 batch_id，可 kg rollback。"""
    batch = "auto-" + time.strftime("%Y%m%d-%H%M%S")
    lines = []
    n_node = 0
    for n in db.list_nodes(conn, status="proposed"):
        sig = db.get_signals(conn, "node", n["id"])
        if not sig or sig.get("llm_verdict") != "独立概念":
            continue
        if not quality.auto_adjudicable(n["source"]):
            continue
        s = sig["signals"]
        if s.get("has_page") and s.get("exact_title"):
            if not s.get("neighbor_overlap"):
                lines.append(f"留人工: 节点「{n['name']}」名字命中语料页"
                             f"但页面与图谱邻居无重叠（疑似撞名）")
                continue
            db.update_node(conn, n["id"], status="approved")
            db.log_review(conn, "node", n["id"], "approve",
                          detail=sig.get("llm_reason") or "", source=n["source"],
                          decided_by="auto", batch_id=batch)
            n_node += 1
            lines.append(f"自动批准节点: {n['name']}")

    nodes = {n["id"]: n for n in db.list_nodes(conn)}
    names = {i: n["name"] for i, n in nodes.items()}
    visible = set(db.visible_statuses())
    # 守卫前移：无环类型的生效邻接表，批一条更新一条，放行不可能引入环
    adj = {t: defaultdict(set) for t in guards.ACYCLIC_TYPES}
    for t in guards.ACYCLIC_TYPES:
        for e in db.approved_edges(conn, t):
            adj[t][e["src"]].add(e["dst"])

    def would_cycle(t, src, dst):
        seen, stack = set(), [dst]
        while stack:
            u = stack.pop()
            if u == src:
                return True
            if u in seen:
                continue
            seen.add(u)
            stack.extend(adj[t][u] - seen)
        return False

    n_approve = n_reject = 0
    for e in db.list_edges(conn, status="proposed"):
        if nodes[e["src"]]["status"] not in visible or nodes[e["dst"]]["status"] not in visible:
            continue
        sig = db.get_signals(conn, "edge", e["id"])
        if not sig or not sig.get("llm_verdict"):
            continue
        if not quality.auto_adjudicable(e["source"]):
            continue
        s = sig["signals"]
        supported = bool(s.get("wikidata") or s.get("link_src_dst") or s.get("link_dst_src")
                         or s.get("toc", 0) > 0)
        refd_against = e["type"] == "prerequisite_of" and s.get("refd", 0) < 0
        toc_against = e["type"] == "prerequisite_of" and s.get("toc", 0) < 0
        if sig["llm_verdict"] == "支持" and supported and not refd_against and not toc_against:
            if e["type"] in adj and would_cycle(e["type"], e["src"], e["dst"]):
                lines.append(f"留人工（批准会成环）: {names[e['src']]} -{e['type']}-> {names[e['dst']]}")
                continue
            conn.execute("UPDATE edges SET status='approved' WHERE id=?", (e["id"],))
            db.log_review(conn, "edge", e["id"], "approve",
                          detail=sig.get("llm_reason") or "", source=e["source"],
                          decided_by="auto", batch_id=batch)
            if e["type"] in adj:
                adj[e["type"]][e["src"]].add(e["dst"])
            n_approve += 1
            lines.append(f"自动批准: {names[e['src']]} -{e['type']}-> {names[e['dst']]}")
        elif sig["llm_verdict"].startswith("不支持") and not supported:
            conn.execute("UPDATE edges SET status='rejected' WHERE id=?", (e["id"],))
            db.log_review(conn, "edge", e["id"], "reject",
                          detail=sig.get("llm_reason") or "", source=e["source"],
                          decided_by="auto", batch_id=batch)
            n_reject += 1
            lines.append(f"自动拒绝: {names[e['src']]} -{e['type']}-> {names[e['dst']]}"
                         f"（{sig.get('llm_reason') or ''}）")
    conn.commit()
    lines.append(f"自动裁决批次 {batch}：节点批准 {n_node}，边批准 {n_approve}，边拒绝 {n_reject}"
                 f"（其余留人工；kg review --audit 抽检，误放行 kg rollback {batch}）")
    return lines


def list_batches(conn) -> list[dict]:
    """自动裁决批次列表（供 kg rollback 不带参数时展示）。"""
    rows = conn.execute(
        "SELECT batch_id, COUNT(*) c,"
        "  SUM(action='approve') approves, SUM(action='reject') rejects,"
        "  MIN(created_at) t FROM review_log"
        " WHERE decided_by='auto' AND batch_id != '' GROUP BY batch_id ORDER BY t DESC")
    return [dict(r) for r in rows]


def rollback_batch(conn, batch_id: str) -> list[str]:
    """整批撤销一次自动裁决：条目退回 proposed（重新排队，人工再裁）。

    跳过此后另有裁决记录的条目（人工已确认/推翻，后来的裁决优先）；
    节点回滚时级联把它名下仍 approved 的边一并退回 proposed
    （端点未生效的边不该保持生效）。回滚本身也写 review_log 留痕。"""
    rows = conn.execute(
        "SELECT * FROM review_log WHERE batch_id=? AND decided_by='auto'"
        " AND action IN ('approve','reject') ORDER BY id", (batch_id,)).fetchall()
    if not rows:
        return [f"没有批次 {batch_id} 的自动裁决记录（kg rollback 不带参数可列出批次）"]
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    lines, n_back, n_skip = [], 0, 0
    for r in rows:
        later = conn.execute(
            "SELECT 1 FROM review_log WHERE item_type=? AND item_id=? AND id>?",
            (r["item_type"], r["item_id"], r["id"])).fetchone()
        label = (f"节点「{names.get(r['item_id'], r['item_id'])}」" if r["item_type"] == "node"
                 else f"边 {r['item_id']}")
        if later:
            n_skip += 1
            lines.append(f"跳过 {label}：此后另有裁决记录（后来的裁决优先）")
            continue
        table = "edges" if r["item_type"] == "edge" else "nodes"
        conn.execute(f"UPDATE {table} SET status='proposed' WHERE id=?", (r["item_id"],))
        db.log_review(conn, r["item_type"], r["item_id"], "rollback",
                      detail=f"批次 {batch_id} 回滚（原 {r['action']}）",
                      source=r["source"], batch_id=batch_id)
        n_back += 1
        lines.append(f"退回 proposed: {label}（原自动 {r['action']}）")
        if r["item_type"] == "node":
            cascade = conn.execute(
                "SELECT id FROM edges WHERE (src=? OR dst=?) AND status='approved'",
                (r["item_id"], r["item_id"])).fetchall()
            for c in cascade:
                conn.execute("UPDATE edges SET status='proposed' WHERE id=?", (c["id"],))
                db.log_review(conn, "edge", c["id"], "rollback",
                              detail=f"批次 {batch_id} 级联（端点节点回滚）", batch_id=batch_id)
                lines.append(f"  级联退回边 {c['id']}（端点节点已回滚）")
    conn.commit()
    lines.append(f"批次 {batch_id}：退回 {n_back} 条，跳过 {n_skip} 条")
    return lines
