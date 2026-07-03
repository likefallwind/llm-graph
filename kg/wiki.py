"""Wikipedia 数据源：搜索 + 整页抓取（正文/版本号/重定向/分类/内链）。"""
import time

import requests

API = {"zh": "https://zh.wikipedia.org/w/api.php",
       "en": "https://en.wikipedia.org/w/api.php"}
HEADERS = {"User-Agent": "llm-graph-kg/0.1 (personal knowledge graph project)"}

MIN_USEFUL_CHARS = 300    # 正文短于此长度视为无效来源
MAX_TEXT_CHARS = 7000     # 单次送给 LLM 的正文上限
MAX_STORE_CHARS = 60000   # 语料库存储的正文上限

REQUEST_INTERVAL = 1.0    # 任意两次 API 请求的最小间隔（秒）
_last_request = 0.0


def _get(lang: str, params: dict) -> dict:
    """统一出口：节流 + 429 退避重试。"""
    global _last_request
    for attempt in range(5):
        wait = REQUEST_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        r = requests.get(API[lang], headers=HEADERS, timeout=60, params=params)
        _last_request = time.time()
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 2 ** (attempt + 1))), 60))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Wikipedia API 限流：重试后仍 429（{lang}）")


def search(term: str, lang: str) -> str | None:
    """返回最匹配的页面标题。"""
    data = _get(lang, {"action": "query", "list": "search", "srsearch": term,
                       "srlimit": 3, "format": "json", "utf8": 1})
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def fetch_page(title: str, lang: str) -> dict | None:
    """整页抓取：正文 + revision_id + 指向本页的重定向 + 分类 + 内链。

    跟随 API 的 continue 分页把 links/categories/redirects 收集完整；
    页面不存在返回 None。
    """
    params = {
        "action": "query", "titles": title, "redirects": 1,
        "format": "json", "utf8": 1,
        "prop": "extracts|revisions|categories|links|redirects",
        "explaintext": 1, "rvprop": "ids",
        "cllimit": "max", "pllimit": "max", "plnamespace": 0,
        "rdlimit": "max", "rdnamespace": 0,
    }
    if lang == "zh":
        params["variant"] = "zh-cn"
    page = {"lang": lang, "text": "", "redirects": [], "categories": [], "links": []}
    cont = {}
    for _ in range(30):  # 分页保险丝
        data = _get(lang, {**params, **cont})
        for pid, p in data.get("query", {}).get("pages", {}).items():
            if int(pid) < 0:
                return None
            page["page_id"] = int(pid)
            page["title"] = p.get("title", title)
            if p.get("extract") and not page["text"]:
                page["text"] = p["extract"][:MAX_STORE_CHARS]
            if p.get("revisions") and "revision_id" not in page:
                page["revision_id"] = p["revisions"][0]["revid"]
            page["redirects"] += [x["title"] for x in p.get("redirects", [])]
            page["categories"] += [x["title"].split(":", 1)[-1] for x in p.get("categories", [])]
            page["links"] += [x["title"] for x in p.get("links", [])]
        if "continue" not in data:
            break
        cont = data["continue"]
    if "page_id" not in page or "revision_id" not in page:
        return None
    return page
