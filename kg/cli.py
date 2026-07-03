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


def cmd_check(args):
    print(guards.run_all(db.connect()))


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
    print(f"来源: {stats['page']}  ({stats['source']})")
    if dry_run:
        print(json.dumps(stats["details"], ensure_ascii=False, indent=2))
        return
    for line in stats["details"]:
        print("  " + line)
    print(f"  新节点 {stats['proposed_nodes']}，合并别名 {stats['merged_aliases']}，"
          f"新边 {stats['proposed_edges']}，丢弃无据 related_to {stats['dropped_related']}，"
          f"锚点新增 facets {stats['anchor_facets_added']}"
          f"（节点与边均为 proposed，待审核）")


def cmd_ingest(args):
    from . import ingest
    conn = db.connect()
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


def cmd_mine(args):
    from . import mine
    conn = db.connect()
    lines = mine.import_aliases(conn) if args.action == "aliases" else mine.category_edges(conn)
    for line in lines:
        print(line)
    print(f"共 {len(lines)} 条" if lines else "无新发现")


def _review_nodes(conn):
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    pending = db.list_nodes(conn, status="proposed")
    print(f"\n=== 待审核节点 {len(pending)} 个 ===")
    for n in pending:
        rel = [f"  {names[e['src']]} -{e['type']}-> {names[e['dst']]} ({e['rationale']})"
               for e in db.list_edges(conn, status="proposed")
               if n["id"] in (e["src"], e["dst"])]
        print(f"\n[{n['id']}] {n['name']}  ({', '.join(n['aliases']) or '无别名'})")
        print(f"  定义: {n['definition']}")
        if n["facets"]:
            print(f"  facets: {', '.join(n['facets'])}")
        print(f"  来源: {n['source']}")
        if rel:
            print("  关联的待审核边:")
            print("\n".join(rel))
        ans = input("  [a]批准 [r]拒绝 [m]合并到已有节点 [s]跳过 [q]退出 > ").strip().lower()
        if ans == "q":
            return False
        if ans == "a":
            db.update_node(conn, n["id"], status="approved")
        elif ans == "r":
            db.update_node(conn, n["id"], status="rejected")
            conn.execute("UPDATE edges SET status='rejected' WHERE (src=? OR dst=?) AND status='proposed'",
                         (n["id"], n["id"]))
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
        conn.commit()
    return True


def _review_edges(conn):
    names = {n["id"]: n["name"] for n in db.list_nodes(conn)}
    status_of = {n["id"]: n["status"] for n in db.list_nodes(conn)}
    pending = [e for e in db.list_edges(conn, status="proposed")
               if status_of.get(e["src"]) in db.visible_statuses()
               and status_of.get(e["dst"]) in db.visible_statuses()]
    print(f"\n=== 待审核边 {len(pending)} 条（两端节点均已生效）===")
    for e in pending:
        print(f"\n[{e['id']}] {names[e['src']]} -{e['type']}-> {names[e['dst']]}"
              f"  (confidence={e['confidence']})")
        print(f"  理由: {e['rationale']}")
        ans = input("  [a]批准 [r]拒绝 [s]跳过 [q]退出 > ").strip().lower()
        if ans == "q":
            return False
        if ans == "a":
            conn.execute("UPDATE edges SET status='approved' WHERE id=?", (e["id"],))
        elif ans == "r":
            conn.execute("UPDATE edges SET status='rejected' WHERE id=?", (e["id"],))
        conn.commit()
    return True


def cmd_review(args):
    conn = db.connect()
    if _review_nodes(conn):
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

    s = sub.add_parser("viz", help="生成可视化 HTML")
    s.add_argument("--out", default="out/graph.html")
    s.add_argument("--approved-only", action="store_true")
    s.set_defaults(fn=cmd_viz)

    s = sub.add_parser("export", help="导出节点邻域 JSON（教学接口）")
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
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("corpus", help="领域语料库：crawl 抓生效节点页面 / grow 沿内链扩展 / stats")
    s.add_argument("action", choices=["crawl", "grow", "stats"])
    s.add_argument("--limit", type=int, help="本次最多抓取页数（grow 默认 10）")
    s.set_defaults(fn=cmd_corpus)

    s = sub.add_parser("mine", help="结构挖掘（零 LLM）：aliases 重定向→别名 / categories 分类→候选边")
    s.add_argument("action", choices=["aliases", "categories"])
    s.set_defaults(fn=cmd_mine)

    s = sub.add_parser("review", help="逐条审核 proposed 节点与边")
    s.set_defaults(fn=cmd_review)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
