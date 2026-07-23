"""CLI 入口：python -m kg <命令>"""
import argparse
import json
import sys

from . import db, guards, seed, viz, export


def cmd_seed(args):
    conn = db.connect()
    stats = seed.load(conn, args.file, with_embeddings=not args.no_embed)
    print(f"导入完成：节点 +{stats['nodes']}（跳过 {stats['nodes_skipped']}），"
          f"边 +{stats['edges']}（跳过 {stats['edges_skipped']}）")
    print(guards.run_all(conn))


def cmd_stats(args):
    conn = db.connect()
    nodes, edges = db.list_nodes(conn), db.list_edges(conn)
    by = lambda items, key: {k: sum(1 for i in items if i[key] == k)
                             for k in sorted({i[key] for i in items})}
    print(f"节点 {len(nodes)}：{by(nodes, 'status')}")
    print(f"边   {len(edges)}：{by(edges, 'status')}")
    print(f"边类型：{by(edges, 'type')}")
    # 裁决日志按通道汇总：这是日后校准自动放行阈值的数据
    rows = conn.execute(
        "SELECT CASE WHEN source LIKE 'mine:%' THEN source"
        "            WHEN source LIKE 'wikidata%' THEN 'wikidata'"
        "            WHEN source LIKE 'doc:%' THEN 'doc:' || substr(source, 5,"
        "                 instr(substr(source, 5), ':') - 1)"
        "            WHEN source LIKE 'wiki:%' THEN 'ingest/expand' ELSE source END AS channel,"
        "       decided_by, action, COUNT(*) c FROM review_log"
        " GROUP BY channel, decided_by, action ORDER BY channel").fetchall()
    if rows:
        print("裁决日志：")
        for r in rows:
            print(f"  {r['channel'] or '(空)'} [{r['decided_by']}] {r['action']}: {r['c']}")


def cmd_check(args):
    print(guards.run_all(db.connect()))


def cmd_calibrate(args):
    from . import calibrate
    print(calibrate.report(db.connect()))


def cmd_viz(args):
    path = viz.export_html(db.connect(), args.out, include_proposed=not args.approved_only)
    print(f"已生成 {path}")


def cmd_export(args):
    data = export.neighborhood(db.connect(), args.name)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_embed(args):
    n = seed.ensure_embeddings(db.connect())
    print(f"补齐 embedding {n} 个")


def cmd_expand(args):
    from . import expand  # 延迟导入，避免无 API key 时其他命令不可用
    conn = db.connect()
    if args.node:
        node = db.find_by_name_or_alias(conn, args.node)
        if not node:
            sys.exit(f"节点不存在: {args.node}")
        targets = [node]
    else:
        targets = expand.pick_frontier(conn, k=args.count)
    for node in targets:
        print(f"== 扩展「{node['name']}」（假设生成器）==")
        stats = expand.expand_node(conn, node, limit=args.limit, dry_run=args.dry_run)
        for line in stats["details"]:
            print("  " + line)
        print(f"  假设 {stats['hypotheses']}，语料验证通过 {stats['verified']}，丢弃 {stats['dropped']}"
              + ("（--dry-run，验证通过的未提取）" if args.dry_run else
                 f"；提取新节点 {stats['proposed_nodes']}，合并别名 {stats['merged_aliases']}，"
                 f"新边 {stats['proposed_edges']}（均为 proposed，待审核）"))


def _print_ingest(stats, dry_run):
    print(f"来源: {stats['page']}  ({stats['source']}，提取 {stats.get('blocks', 1)} 块)")
    if dry_run:
        print(json.dumps(stats["details"], ensure_ascii=False, indent=2))
        return
    for line in stats["details"]:
        print("  " + line)
    print(f"  新节点 {stats['proposed_nodes']}，合并别名 {stats['merged_aliases']}，"
          f"降级 facet {stats.get('demoted_facets', 0)}，"
          f"新边 {stats['proposed_edges']}，丢弃无据 related_to {stats['dropped_related']}，"
          f"丢弃伪证据 {stats['dropped_no_evidence']}（重引挽回 {stats.get('requoted', 0)}），"
          f"锚点新增 facets {stats['anchor_facets_added']}，"
          f"误区 {stats.get('misconceptions', 0)}"
          f"（节点与边均为 proposed，待审核）")


def cmd_ingest(args):
    from . import ingest
    conn = db.connect()
    if args.batch and args.doc is not None:
        # 教材是 high 级语料（教学性关系密度最高），批量提取优先走这里
        picks = ingest.pick_anchors_doc(conn, k=args.batch, book=args.doc or None)
        if not picks:
            sys.exit("没有可选教材锚点（先 kg docs fetch，或匹配到的章节版本都已提取过）")
        for p in picks:
            sec = p["section"]
            print(f"== 教材锚点「{p['node']['name']}」（{sec['book']} §{sec['sec_id']}"
                  f"「{sec['title']}」，{'标题命中' if p['how'] == 'title' else '正文命中'}，"
                  f"缺口分 {p['score']:.2f}）==")
            stats = ingest.ingest_topic_doc(conn, p["node"]["name"], book=sec["book"],
                                            limit=args.limit, dry_run=args.dry_run)
            if stats.get("error"):
                print("  " + stats["error"])
                continue
            _print_ingest(stats, args.dry_run)
        return
    if args.batch:
        picks = ingest.pick_anchors(conn, k=args.batch)
        if not picks:
            sys.exit("没有可选锚点（语料库为空？先跑 kg corpus crawl）")
        for p in picks:
            print(f"== 锚点「{p['node']['name']}」（内链入度 {p['indegree']}，缺口分 {p['score']:.1f}）==")
            stats = ingest.ingest_topic(conn, p["node"]["name"], limit=args.limit, dry_run=args.dry_run)
            if stats.get("error"):
                print("  " + stats["error"])
                continue
            _print_ingest(stats, args.dry_run)
        return
    if not args.name:
        sys.exit("用法：kg ingest <锚点名> 或 kg ingest --batch N")
    if args.doc is not None:
        stats = ingest.ingest_topic_doc(conn, args.name, book=args.doc or None,
                                        limit=args.limit, dry_run=args.dry_run)
    else:
        stats = ingest.ingest_topic(conn, args.name, limit=args.limit, dry_run=args.dry_run)
    if stats.get("error"):
        sys.exit(stats["error"])
    _print_ingest(stats, args.dry_run)


def cmd_corpus(args):
    from . import corpus
    conn = db.connect()
    if args.action == "crawl":
        for line in corpus.crawl(conn, limit=args.limit):
            print(line)
    elif args.action == "grow":
        for line in corpus.grow(conn, limit=args.limit or 10):
            print(line)
    print(corpus.stats(conn))


def cmd_docs(args):
    from . import docs
    conn = db.connect()
    if args.action == "add":
        if not args.target:
            sys.exit("用法：kg docs add sources/<book>.yaml")
        info = docs.register(conn, docs.load_config(args.target))
        print(f"已登记 {info['book']}：章节 {info['sections']}（本次新增 {info['added']}）")
    elif args.action == "fetch":
        if not args.target:
            sys.exit("用法：kg docs fetch <book>")
        for line in docs.fetch(conn, args.target, limit=args.limit, sec_id=args.sec):
            print(line)
    elif args.action == "translate":
        if not args.target:
            sys.exit("用法：kg docs translate <book>")
        for line in docs.translate(conn, args.target, limit=args.limit, sec_id=args.sec):
            print(line)
    print(docs.stats(conn))


def cmd_mine(args):
    from . import mine, wikidata
    conn = db.connect()
    if args.action == "aliases":
        lines = mine.import_aliases(conn)
    elif args.action == "categories":
        lines = mine.category_edges(conn)
    else:
        lines = wikidata.mine_edges(conn, create_spine=args.spine)
    for line in lines:
        print(line)
    print(f"共 {len(lines)} 条" if lines else "无新发现")


def _signal_line(conn, item_type, item_id) -> str:
    """review_signals 里若有佐证（kg verify 产出），拼一行展示。"""
    sig = db.get_signals(conn, item_type, item_id)
    if not sig:
        return ""
    parts = []
    s = sig["signals"]
    if item_type == "edge":
        if "link_src_dst" in s:
            arrow = {(True, True): "互链", (True, False): "仅 src→dst",
                     (False, True): "仅 dst→src", (False, False): "无互链"}
            parts.append("语料链接: " + arrow[(bool(s["link_src_dst"]), bool(s["link_dst_src"]))])
        if s.get("refd"):
            parts.append(f"RefD 先修信号: {'支持' if s['refd'] > 0 else '方向可疑'}")
        if s.get("toc"):
            parts.append(f"教材目录序: {'支持' if s['toc'] > 0 else '方向可疑'}")
        if s.get("wikidata"):
            parts.append(f"Wikidata: {s['wikidata']}")
    if item_type == "node" and "has_page" in s:
        if s["has_page"]:
            hint = "有语料页"
            if s.get("exact_title"):
                hint += "（名字精确命中标题/重定向）"
            if "neighbor_overlap" in s:
                hint += "（与图谱邻居" + ("有重叠" if s["neighbor_overlap"]
                                          else "无重叠，疑似撞名") + "）"
            parts.append(hint)
        else:
            parts.append("无语料页")
    if sig.get("llm_verdict"):
        parts.append(f"LLM 复核: {sig['llm_verdict']}（{sig.get('llm_reason') or ''}）")
    return "；".join(parts)


def _triage(conn, item_type, item) -> tuple[int, str]:
    """按复核信号给待审条目分层，人工时间优先花在信号冲突上。
    0=信号冲突/需人裁决（最该看） 1=证据不足/未复核 2=信号弱一致（大概率没问题）。"""
    sig = db.get_signals(conn, item_type, item["id"])
    if not sig or not sig.get("llm_verdict"):
        return 1, "未复核"
    v, s = sig["llm_verdict"], sig["signals"]
    if item_type == "node":
        if v.startswith("应为facet"):
            return 0, v  # 降级需要人选归属概念
        if v == "独立概念":
            if s.get("has_page") and s.get("exact_title"):
                if s.get("neighbor_overlap") is False:
                    return 0, "名字命中语料页但页面与图谱邻居无重叠（疑似撞名）"
                return 2, "信号弱一致"  # 没被 --apply 放行说明还没跑，或刚复核完
            return 0, "LLM 判独立概念但名字未命中语料页"
        return 1, "证据不足"
    supported = bool(s.get("wikidata") or s.get("link_src_dst") or s.get("link_dst_src")
                     or s.get("toc", 0) > 0)
    against = item["type"] == "prerequisite_of" and (s.get("refd", 0) < 0 or s.get("toc", 0) < 0)
    if "方向存疑" in v or (v.startswith("支持") and against) \
            or (v.startswith("不支持") and supported):
        return 0, "信号冲突"
    if v.startswith("证据不足"):
        return 1, "证据不足"
    return 2, "信号弱一致"


_TIER_NAMES = {0: "信号冲突", 1: "证据不足/未复核", 2: "信号弱一致"}


def _tier_summary(tiers) -> str:
    counts = {}
    for t, _ in tiers.values():
        counts[t] = counts.get(t, 0) + 1
    return "，".join(f"{_TIER_NAMES[t]} {counts[t]}" for t in sorted(counts)) or "空"


def _review_nodes(conn):
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    pending = db.list_nodes(conn, status="proposed")
    tiers = {n["id"]: _triage(conn, "node", n) for n in pending}
    pending.sort(key=lambda n: (tiers[n["id"]][0], n["id"]))
    print(f"\n=== 待审核节点 {len(pending)} 个（{_tier_summary(tiers)}）===")
    for n in pending:
        rel = [f"  {names[e['src']]} -{e['type']}-> {names[e['dst']]} ({e['rationale']})"
               for e in db.list_edges(conn, status="proposed")
               if n["id"] in (e["src"], e["dst"])]
        print(f"\n[{n['id']}] {n['name']}  ({', '.join(n['aliases']) or '无别名'})"
              f"  〔{tiers[n['id']][1]}〕")
        print(f"  定义: {n['definition']}")
        if n["facets"]:
            print(f"  facets: {', '.join(n['facets'])}")
        print(f"  来源: {n['source']}")
        hint = _signal_line(conn, "node", n["id"])
        if hint:
            print(f"  佐证: {hint}")
        if rel:
            print("  关联的待审核边:")
            print("\n".join(rel))
        ans = input("  [a]批准 [r]拒绝 [m]合并到已有节点 [d]降级为已有节点的facet [s]跳过 [q]退出 > ").strip().lower()
        if ans == "q":
            return False
        if ans == "a":
            db.update_node(conn, n["id"], status="approved")
            db.log_review(conn, "node", n["id"], "approve", source=n["source"])
            conn.commit()
            # 节点生效后其关联边立即具备裁决条件，就地顺带裁决——
            # 上下文还在审核者脑子里，不必等到边审核阶段重新建立
            status_of = {x["id"]: x["status"] for x in db.list_nodes(conn)}
            related = [e for e in db.list_edges(conn, status="proposed")
                       if n["id"] in (e["src"], e["dst"])
                       and status_of[e["src"]] in db.visible_statuses()
                       and status_of[e["dst"]] in db.visible_statuses()]
            if related:
                print(f"  已批准；就地裁决其 {len(related)} 条关联边（另一端已生效）:")
                for e in related:
                    if not _adjudicate_edge(conn, e, names):
                        return False
        elif ans == "r":
            db.update_node(conn, n["id"], status="rejected")
            conn.execute("UPDATE edges SET status='rejected' WHERE (src=? OR dst=?) AND status='proposed'",
                         (n["id"], n["id"]))
            db.log_review(conn, "node", n["id"], "reject", source=n["source"])
        elif ans == "m":
            target_name = input("  合并到（节点名）> ").strip()
            target = db.find_by_name_or_alias(conn, target_name)
            if not target or target["id"] == n["id"]:
                print("  目标节点无效，跳过")
                continue
            from . import dedup
            dedup.merge_as_alias(conn, target, n["name"])
            conn.execute("UPDATE edges SET src=? WHERE src=?", (target["id"], n["id"]))
            conn.execute("UPDATE edges SET dst=? WHERE dst=?", (target["id"], n["id"]))
            db.update_node(conn, n["id"], status="rejected")
            db.log_review(conn, "node", n["id"], "merge",
                          detail=f"-> {target['name']}", source=n["source"])
        elif ans == "d":
            target_name = input("  作为哪个节点的 facet（节点名）> ").strip()
            target = db.find_by_name_or_alias(conn, target_name)
            if not target or target["id"] == n["id"]:
                print("  目标节点无效，跳过")
                continue
            if n["name"] not in target["facets"]:
                db.update_node(conn, target["id"], facets=target["facets"] + [n["name"]])
            db.update_node(conn, n["id"], status="rejected")
            conn.execute("UPDATE edges SET status='rejected' WHERE (src=? OR dst=?) AND status='proposed'",
                         (n["id"], n["id"]))
            db.log_review(conn, "node", n["id"], "demote",
                          detail=f"facet of {target['name']}", source=n["source"])
            print(f"  已降级为「{target['name']}」的 facet")
        conn.commit()
    return True


def _adjudicate_edge(conn, e, names) -> bool:
    """单条边的交互裁决（review 边主循环与节点批准后的就地裁决共用）。返回 False=退出。"""
    tier = _triage(conn, "edge", e)
    print(f"\n[{e['id']}] {names[e['src']]} -{e['type']}-> {names[e['dst']]}"
          f"  (confidence={e['confidence']})  〔{tier[1]}〕")
    print(f"  理由: {e['rationale']}")
    hint = _signal_line(conn, "edge", e["id"])
    if hint:
        print(f"  佐证: {hint}")
    ans = input("  [a]批准 [r]拒绝 [f]方向反了(翻转并批准) [t]改类型并批准 [s]跳过 [q]退出 > ").strip().lower()
    if ans == "q":
        return False
    if ans == "a":
        conn.execute("UPDATE edges SET status='approved' WHERE id=?", (e["id"],))
        db.log_review(conn, "edge", e["id"], "approve", source=e["source"])
    elif ans == "r":
        conn.execute("UPDATE edges SET status='rejected' WHERE id=?", (e["id"],))
        db.log_review(conn, "edge", e["id"], "reject", source=e["source"])
    elif ans == "f":
        dup = conn.execute("SELECT id FROM edges WHERE src=? AND dst=? AND type=?",
                           (e["dst"], e["src"], e["type"])).fetchone()
        if dup:
            print(f"  反向边已存在（id={dup['id']}），本条按拒绝处理")
            conn.execute("UPDATE edges SET status='rejected' WHERE id=?", (e["id"],))
            db.log_review(conn, "edge", e["id"], "reject",
                          detail=f"方向反且反向边已存在 id={dup['id']}", source=e["source"])
        else:
            conn.execute("UPDATE edges SET src=?, dst=?, status='approved',"
                         " rationale=? WHERE id=?",
                         (e["dst"], e["src"], f"[人工翻转] {e['rationale']}", e["id"]))
            db.log_review(conn, "edge", e["id"], "flip", source=e["source"])
            print(f"  已翻转: {names[e['dst']]} -{e['type']}-> {names[e['src']]}")
    elif ans == "t":
        new_type = input(f"  新类型（{'/'.join(db.EDGE_TYPES)}）> ").strip()
        if new_type not in db.EDGE_TYPES or new_type == e["type"]:
            print("  类型无效，跳过")
            return True
        rationale = e["rationale"]
        if new_type == "related_to":
            kind = input(f"  kind（{'/'.join(db.RELATED_KINDS)}）> ").strip()
            if kind not in db.RELATED_KINDS:
                print("  kind 无效，跳过")
                return True
            rationale = f"[{kind}] {rationale}"
        dup = conn.execute("SELECT id FROM edges WHERE src=? AND dst=? AND type=? AND id!=?",
                           (e["src"], e["dst"], new_type, e["id"])).fetchone()
        if dup:
            print(f"  同型边已存在（id={dup['id']}），本条按拒绝处理")
            conn.execute("UPDATE edges SET status='rejected' WHERE id=?", (e["id"],))
            db.log_review(conn, "edge", e["id"], "reject",
                          detail=f"改型后与 id={dup['id']} 重复", source=e["source"])
        else:
            conn.execute("UPDATE edges SET type=?, status='approved',"
                         " rationale=? WHERE id=?",
                         (new_type, f"[人工改型 {e['type']}->{new_type}] {rationale}", e["id"]))
            db.log_review(conn, "edge", e["id"], "retype",
                          detail=f"{e['type']}->{new_type}", source=e["source"])
    conn.commit()
    return True


def _review_edges(conn):
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    status_of = {n["id"]: n["status"] for n in db.list_nodes(conn)}
    pending = [e for e in db.list_edges(conn, status="proposed")
               if status_of.get(e["src"]) in db.visible_statuses()
               and status_of.get(e["dst"]) in db.visible_statuses()]
    tiers = {e["id"]: _triage(conn, "edge", e) for e in pending}
    pending.sort(key=lambda e: (tiers[e["id"]][0], e["id"]))
    print(f"\n=== 待审核边 {len(pending)} 条（两端节点均已生效；{_tier_summary(tiers)}）===")
    for e in pending:
        if not _adjudicate_edge(conn, e, names):
            return False
    return True


def _review_audit(conn, k):
    """抽检自动放行的条目：AI 为主审后，人工的角色从守门员变成抽检员。"""
    import random
    rows = conn.execute(
        "SELECT * FROM review_log WHERE decided_by='auto' AND action='approve'"
        " AND NOT EXISTS (SELECT 1 FROM review_log h WHERE h.item_type=review_log.item_type"
        "   AND h.item_id=review_log.item_id AND h.action LIKE 'audit_%')").fetchall()
    if not rows:
        print("没有可抽检的自动放行条目")
        return
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    sample = random.sample(rows, min(k, len(rows)))
    print(f"=== 抽检 {len(sample)}/{len(rows)} 条自动放行 ===")
    for r in sample:
        if r["item_type"] == "edge":
            e = conn.execute("SELECT * FROM edges WHERE id=?", (r["item_id"],)).fetchone()
            if not e or e["status"] != "approved":
                continue
            print(f"\n[edge {e['id']}] {names[e['src']]} -{e['type']}-> {names[e['dst']]}")
            print(f"  理由: {e['rationale']}")
        else:
            n = db.get_node(conn, r["item_id"])
            if not n or n["status"] != "approved":
                continue
            print(f"\n[node {n['id']}] {n['name']}: {n['definition']}")
        ans = input("  [y]无误 [r]误放行，改为拒绝 [s]跳过 [q]退出 > ").strip().lower()
        if ans == "q":
            break
        if ans == "y":
            db.log_review(conn, r["item_type"], r["item_id"], "audit_confirm", source=r["source"])
        elif ans == "r":
            table = "edges" if r["item_type"] == "edge" else "nodes"
            conn.execute(f"UPDATE {table} SET status='rejected' WHERE id=?", (r["item_id"],))
            db.log_review(conn, r["item_type"], r["item_id"], "audit_reject", source=r["source"])
        conn.commit()


def cmd_verify(args):
    from . import verify
    conn = db.connect()
    for line in verify.structural_signals(conn):
        print(line)
    if not args.no_llm:
        for line in verify.llm_review(conn, limit=args.limit, redo=args.redo):
            print("  " + line)
    if args.apply:
        for line in verify.apply_auto(conn, dry_run=args.dry_run):
            print(line)
    elif args.dry_run:
        print("--dry-run 需要与 --apply 连用（影子裁决）")


def cmd_rollback(args):
    from . import verify
    conn = db.connect()
    if not args.batch_id:
        batches = verify.list_batches(conn)
        if not batches:
            print("没有自动裁决批次记录")
            return
        print("自动裁决批次（新在前）：")
        import datetime
        for b in batches:
            t = datetime.datetime.fromtimestamp(b["t"]).strftime("%Y-%m-%d %H:%M")
            print(f"  {b['batch_id']}  [{t}]  批准 {b['approves']}，拒绝 {b['rejects']}")
        print("用法：kg rollback <batch_id> 整批退回 proposed 重新人工审")
        return
    for line in verify.rollback_batch(conn, args.batch_id):
        print(line)
    print("\n回滚后运行一致性守卫：")
    print(guards.run_all(conn))


def cmd_review(args):
    conn = db.connect()
    if args.audit:
        _review_audit(conn, args.audit)
    elif _review_nodes(conn):
        _review_edges(conn)
    print("\n审核结束，运行一致性守卫：")
    print(guards.run_all(conn))


def main():
    p = argparse.ArgumentParser(prog="kg", description="自进化知识图谱 MVP")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="导入种子 YAML")
    s.add_argument("file")
    s.add_argument("--no-embed", action="store_true", help="跳过 embedding 计算")
    s.set_defaults(fn=cmd_seed)

    s = sub.add_parser("stats", help="统计")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("check", help="一致性守卫")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("calibrate", help="校准统计：人工裁决 precision（通道×类型×佐证组合）"
                                         "+ 自动放行抽检推翻率")
    s.set_defaults(fn=cmd_calibrate)

    s = sub.add_parser("viz", help="生成可视化 HTML")
    s.add_argument("--out", default="out/graph.html")
    s.add_argument("--approved-only", action="store_true")
    s.set_defaults(fn=cmd_viz)

    s = sub.add_parser("export", help="导出节点邻域 JSON（教学接口：定义/facets/误区/先修链/讲解资源）")
    s.add_argument("name")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("embed", help="补齐缺失的 embedding")
    s.set_defaults(fn=cmd_embed)

    s = sub.add_parser("expand", help="假设生成器（LLM 提缺口名字 -> 语料验证 -> 转有据提取）")
    s.add_argument("--node", help="指定节点名；缺省自动选前沿节点")
    s.add_argument("--count", type=int, default=1, help="自动选取的前沿节点数")
    s.add_argument("--limit", type=int, default=5, help="每个节点最多提议数")
    s.add_argument("--dry-run", action="store_true", help="只做假设+语料验证，不提取入库")
    s.set_defaults(fn=cmd_expand)

    s = sub.add_parser("ingest", help="从语料库围绕已有节点提取知识（有据可查，主通道）")
    s.add_argument("name", nargs="?", help="锚点节点名（须已存在于图谱）")
    s.add_argument("--batch", type=int, help="缺口驱动自动选 N 个锚点批量提取")
    s.add_argument("--limit", type=int, default=6, help="每个锚点最多提取的概念数")
    s.add_argument("--dry-run", action="store_true", help="只打印提取结果，不入库")
    s.add_argument("--doc", nargs="?", const="", metavar="BOOK",
                   help="从文档语料（教材/教案，high 级语料）提取；可指定 book slug，"
                        "缺省在所有书里找章节；与 --batch 组合为教材缺口驱动批量")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("corpus", help="领域语料库：crawl 抓生效节点页面 / grow 沿内链扩展 / stats")
    s.add_argument("action", choices=["crawl", "grow", "stats"])
    s.add_argument("--limit", type=int, help="本次最多抓取页数（grow 默认 10）")
    s.set_defaults(fn=cmd_corpus)

    s = sub.add_parser("docs", help="文档语料通道：add 登记源配置 / fetch 抓章节 / translate 翻译英文源 / stats")
    s.add_argument("action", choices=["add", "fetch", "translate", "stats"])
    s.add_argument("target", nargs="?", help="add: 配置文件路径；fetch/translate: book slug")
    s.add_argument("--limit", type=int, help="本次最多处理的章节数")
    s.add_argument("--sec", help="只处理指定章节号（fetch 时强制重抓）")
    s.set_defaults(fn=cmd_docs)

    s = sub.add_parser("mine", help="结构挖掘（零 LLM）：aliases 重定向→别名 / categories 分类→候选边"
                                    " / wikidata QID关系→候选边+同概念仲裁")
    s.add_argument("action", choices=["aliases", "categories", "wikidata"])
    s.add_argument("--spine", action="store_true",
                   help="wikidata 专用：给现有节点的 P279/P361 上位类补建脊节点（落地语料），把孤岛接回主干")
    s.set_defaults(fn=cmd_mine)

    s = sub.add_parser("review", help="逐条审核 proposed 节点与边（--audit N 抽检自动放行的条目）")
    s.add_argument("--audit", type=int, help="抽检 N 条自动放行的条目")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("verify", help="复核 proposed 条目：结构佐证（零 LLM）+ LLM 判断题复核；"
                                      "--apply 双重一致自动裁决")
    s.add_argument("--limit", type=int, default=10, help="本次 LLM 复核条数上限")
    s.add_argument("--no-llm", action="store_true", help="只算结构佐证")
    s.add_argument("--redo", action="store_true", help="重跑已有 LLM 结论的条目")
    s.add_argument("--apply", action="store_true",
                   help="双重一致自动裁决（带批次号可回滚）：边批准/拒绝（无环类型先查环）；"
                        "节点仅批准（独立概念+名字命中语料页+与图谱邻居有重叠）")
    s.add_argument("--dry-run", action="store_true",
                   help="与 --apply 连用：影子裁决（burn-in 校准），只记 auto_would 不改状态，"
                        "人工审完后 kg calibrate 看一致率")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("rollback", help="整批撤销一次 verify --apply 自动裁决"
                                        "（不带参数列出批次；条目退回 proposed 重新人工审）")
    s.add_argument("batch_id", nargs="?", help="批次号（verify --apply 输出里的 auto-...）")
    s.set_defaults(fn=cmd_rollback)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
