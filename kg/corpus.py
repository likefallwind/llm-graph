"""领域定向语料库：维基页面本地缓存（含 revision 快照）+ 内链统计。

图长到哪、语料圈到哪：crawl 把生效节点的页面抓进来，grow 沿内链频次扩展。
所有下游抽取都从这里读文本，source 记 wiki:<lang>:<title>@<revision_id>，可复现。
"""
import difflib
import json
import time
from collections import Counter

from . import db, wiki

MATCH_THRESHOLD = 0.55  # 查询词与页面标题/重定向的最低相似度，防搜索引擎乱配


def _similar(a: str, b: str) -> float:
    a, b = a.lower().replace(" ", ""), b.lower().replace(" ", "")
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()



def _row_to_page(row, with_text=True) -> dict:
    d = dict(row)
    for k in ("redirects", "categories", "links"):
        d[k] = json.loads(d[k])
    if not with_text:
        d.pop("text", None)
    return d


def save_page(conn, page: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO corpus"
        "(lang, page_id, title, revision_id, text, redirects, categories, links, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (page["lang"], page["page_id"], page["title"], page["revision_id"], page["text"],
         json.dumps(page["redirects"], ensure_ascii=False),
         json.dumps(page["categories"], ensure_ascii=False),
         json.dumps(page["links"], ensure_ascii=False), time.time()))
    conn.commit()


def all_pages(conn, with_text=False):
    cols = "*" if with_text else \
        "id, lang, page_id, title, revision_id, redirects, categories, links, fetched_at"
    return [_row_to_page(r, with_text) for r in conn.execute(f"SELECT {cols} FROM corpus")]


def get_page(conn, lang: str, title: str, with_text=True):
    row = conn.execute("SELECT * FROM corpus WHERE lang=? AND title=? COLLATE NOCASE",
                       (lang, title)).fetchone()
    return _row_to_page(row, with_text) if row else None


def title_index(conn) -> dict:
    """{标题/重定向(小写): (lang, title)}，zh 优先（后写入覆盖 en）。"""
    idx = {}
    for lang in ("en", "zh"):
        for p in all_pages(conn):
            if p["lang"] != lang:
                continue
            idx[p["title"].lower()] = (p["lang"], p["title"])
            for r in p["redirects"]:
                idx[r.lower()] = (p["lang"], p["title"])
    return idx


def find_page(conn, name: str, index=None, with_text=True):
    """按标题或重定向名在语料库里找页面。"""
    index = index if index is not None else title_index(conn)
    hit = index.get(name.strip().lower())
    return get_page(conn, hit[0], hit[1], with_text) if hit else None


def map_node(conn, node_id: int, page: dict) -> None:
    """记录节点↔页面关联（搜索是模糊匹配，名字未必等于标题/重定向，必须显式记）。"""
    conn.execute("INSERT OR REPLACE INTO node_page(node_id, lang, page_id) VALUES (?,?,?)",
                 (node_id, page["lang"], page["page_id"]))
    conn.commit()


def page_for_node(conn, node: dict, index=None, with_text=True):
    """图谱节点 -> 语料页面：先查显式映射，再按 name/aliases 匹配标题与重定向。"""
    row = conn.execute("SELECT lang, page_id FROM node_page WHERE node_id=?",
                       (node["id"],)).fetchone()
    if row:
        hit = conn.execute("SELECT * FROM corpus WHERE lang=? AND page_id=?",
                           (row["lang"], row["page_id"])).fetchone()
        if hit:
            return _row_to_page(hit, with_text)
    index = index if index is not None else title_index(conn)
    for q in [node["name"]] + node["aliases"]:
        page = find_page(conn, q, index, with_text)
        if page:
            map_node(conn, node["id"], page)
            return page
    return None


def node_for_page(conn, page: dict):
    """语料页面 -> 图谱节点（页面标题或任一重定向命中节点名/别名）。"""
    names = {page["title"].lower(), *(r.lower() for r in page["redirects"])}
    for n in db.list_nodes(conn):
        if n["status"] == "rejected":
            continue
        if n["name"].lower() in names or any(a.lower() in names for a in n["aliases"]):
            return n
    return None


def fetch_and_store(conn, term: str):
    """搜索并抓取一页入库（zh 优先，en 兜底）。返回页面或 None。

    搜索是全文模糊匹配，可能返回毫不相干的页面（如「Xavier初始化」搜到病毒条目），
    因此页面标题或任一重定向必须与查询词足够相似才收。
    """
    for lang in ("zh", "en"):
        title = wiki.search(term, lang)
        if not title:
            continue
        page = get_page(conn, lang, title)
        fresh = page is None
        if fresh:
            page = wiki.fetch_page(title, lang)
        if not page or len(page["text"]) < wiki.MIN_USEFUL_CHARS:
            continue
        if max(_similar(term, t) for t in [page["title"]] + page["redirects"]) < MATCH_THRESHOLD:
            continue  # 搜到的页面和查询词对不上，宁可不要
        if fresh:
            # search 可能返回重定向名，fetch 已归一化到正式标题，再查一次缓存
            cached = get_page(conn, lang, page["title"])
            if cached:
                return cached
            save_page(conn, page)
        return page
    return None


def crawl(conn, limit=None) -> list[str]:
    """把所有生效节点缺失的页面抓进语料库。返回日志行。"""
    lines, fetched = [], 0
    index = title_index(conn)
    for node in db.list_nodes(conn):
        if node["status"] not in db.visible_statuses():
            continue
        if page_for_node(conn, node, index, with_text=False):
            continue
        if limit is not None and fetched >= limit:
            lines.append(f"（达到 --limit {limit}，剩余节点下次继续）")
            break
        page = fetch_and_store(conn, node["name"])
        if not page:
            for alias in node["aliases"]:
                page = fetch_and_store(conn, alias)
                if page:
                    break
        fetched += 1
        if page:
            map_node(conn, node["id"], page)
            index[page["title"].lower()] = (page["lang"], page["title"])
            for r in page["redirects"]:
                index[r.lower()] = (page["lang"], page["title"])
            lines.append(f"✓ {node['name']} -> {source_of(page)}（{len(page['text'])} 字）")
        else:
            lines.append(f"✗ {node['name']} 无有效页面")
    return lines


def link_counts(conn) -> Counter:
    """全语料内链计数：{(lang, 目标标题小写): 被链次数}。"""
    counts = Counter()
    for p in all_pages(conn):
        for t in set(p["links"]):
            counts[(p["lang"], t.lower())] += 1
    return counts


def grow(conn, limit=10) -> list[str]:
    """沿内链频次扩展语料：抓取被引最多、尚不在语料库里的页面。"""
    index = title_index(conn)
    counts = link_counts(conn)
    candidates = [((lang, title), c) for (lang, title), c in counts.most_common()
                  if title not in index]
    lines, fetched = [], 0
    for (lang, title), c in candidates:
        if fetched >= limit:
            break
        page = wiki.fetch_page(title, lang)
        fetched += 1
        if page and len(page["text"]) >= wiki.MIN_USEFUL_CHARS:
            save_page(conn, page)
            lines.append(f"✓ {title}（被 {c} 页链接）-> {source_of(page)}")
        else:
            lines.append(f"✗ {title} 无有效正文，跳过")
    return lines


def source_of(page: dict) -> str:
    return f"wiki:{page['lang']}:{page['title']}@{page['revision_id']}"


def url_of(page: dict) -> str:
    return f"https://{page['lang']}.wikipedia.org/wiki/{page['title'].replace(' ', '_')}"


def stats(conn) -> str:
    pages = all_pages(conn)
    if not pages:
        return "语料库为空（先跑 kg corpus crawl）"
    by_lang = Counter(p["lang"] for p in pages)
    n_links = sum(len(p["links"]) for p in pages)
    n_redirects = sum(len(p["redirects"]) for p in pages)
    index = title_index(conn)
    covered = sum(1 for n in db.list_nodes(conn)
                  if n["status"] in db.visible_statuses()
                  and page_for_node(conn, n, index, with_text=False))
    total = sum(1 for n in db.list_nodes(conn) if n["status"] in db.visible_statuses())
    return (f"语料页面 {len(pages)}（{dict(by_lang)}），内链 {n_links}，重定向 {n_redirects}\n"
            f"生效节点覆盖率 {covered}/{total}")
