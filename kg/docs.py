"""文档语料通道：教材/课程讲义的本地快照（含章节顺序）。

与 corpus.py（维基）平行的第二个语料层，为图谱补充教学性语料：
- 源用声明式配置（sources/<book>.yaml：章节列表 + 定位串），加源不改代码；
- 英文源用翻译模型（llm.TRANSLATE_MODEL）译为中文后入库——翻译文本写入即快照，
  content_hash 是版本号，下游 evidence 对翻译文本做子串校验，不变式不破坏；
- 章节顺序（ord 列）天然编码教学先修顺序，是 verify 的 toc 结构信号来源
  （LLM 零样本判先修不可靠，教材目录是更硬的独立信源）；
- source 格式 doc:<book>:<sec_id>@<content_hash>，与 wiki:<lang>:<title>@<rev> 平行。
"""
import hashlib
import os
import re
import shutil
import subprocess
import time

import requests
import yaml

from . import corpus, db, htmltext, llm

SOURCES_DIR = os.path.join(os.path.dirname(__file__), "..", "sources")
PDF_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "docs")  # PDF 只下载一次
MIN_SECTION_CHARS = 200        # 短于此视为抓取失败（导航页/空页）
TRANSLATE_CHUNK_CHARS = 3000   # 单次翻译块上限（非 reasoning 模型，块小防截断）
HASH_LEN = 12
TITLE_MATCH_THRESHOLD = corpus.MATCH_THRESHOLD  # 节标题↔节点名相似度门槛，与维基一致
MIN_NAME_CHARS = 3             # 短于此的名字不做正文命中（「GRU」类短名撞车风险高）

_HEADERS = {"User-Agent": "kg-docs/0.1 (educational knowledge graph)"}

TRANSLATE_PROMPT = """你是资深技术翻译，把下面的 AI/机器学习教材节选翻译成简体中文。
规则：
1. 保留原有段落与列表结构，不合并、不增删内容；
2. 术语首次出现时在译文后加英文原文括号，如「反向传播（backpropagation）」；
3. 数学公式、代码、变量名原样保留；
4. 只输出译文本身，不要任何说明。"""


# ---------- 源配置 ----------

def config_path(book: str) -> str:
    return os.path.join(SOURCES_DIR, f"{book}.yaml")


def load_config(path: str) -> dict:
    """读并校验源配置。sections 每项 [ord, sec_id, 标题, 定位串]。"""
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("book", "title", "lang", "type", "sections"):
        if not cfg.get(key):
            raise ValueError(f"源配置缺少字段: {key}")
    if not re.fullmatch(r"[a-z0-9-]+", cfg["book"]):
        raise ValueError(f"book slug 只能是 [a-z0-9-]（source 解析依赖）: {cfg['book']}")
    if cfg["type"] not in ("html", "pdf"):
        raise ValueError(f"type 必须是 html 或 pdf: {cfg['type']}")
    if cfg["type"] == "pdf" and not cfg.get("pdf_url"):
        raise ValueError("pdf 源必须提供顶层 pdf_url")
    for s in cfg["sections"]:
        if len(s) != 4:
            raise ValueError(f"节格式应为 [ord, sec_id, 标题, 定位串]: {s}")
    return cfg


def load_book(book: str) -> dict:
    path = config_path(book)
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到源配置 {path}（先 kg docs add 或检查 book 名）")
    return load_config(path)


def register(conn, cfg: dict) -> dict:
    """把配置里的章节元数据 upsert 进 doc_sections（不抓正文，正文归 fetch）。"""
    before = conn.execute("SELECT COUNT(*) c FROM doc_sections WHERE book=?",
                          (cfg["book"],)).fetchone()["c"]
    for ord_, sec_id, title, locator in cfg["sections"]:
        conn.execute(
            "INSERT INTO doc_sections(book, ord, sec_id, title, url, orig_lang)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(book, sec_id) DO UPDATE SET ord=excluded.ord,"
            "   title=excluded.title, url=excluded.url",
            (cfg["book"], int(ord_), str(sec_id), title, locator, cfg["lang"]))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM doc_sections WHERE book=?",
                     (cfg["book"],)).fetchone()["c"]
    return {"book": cfg["book"], "sections": n, "added": n - before}


# ---------- 读取 ----------

def get_section(conn, book: str, sec_id: str):
    row = conn.execute("SELECT * FROM doc_sections WHERE book=? AND sec_id=?",
                       (book, str(sec_id))).fetchone()
    return dict(row) if row else None


def sections(conn, book=None) -> list[dict]:
    q, args = "SELECT * FROM doc_sections", []
    if book:
        q += " WHERE book=?"
        args.append(book)
    return [dict(r) for r in conn.execute(q + " ORDER BY book, ord", args)]


def source_of(sec: dict) -> str:
    return f"doc:{sec['book']}:{sec['sec_id']}@{sec['content_hash']}"


def parse_source(source: str):
    """doc:<book>:<sec_id>@<hash> -> (book, sec_id, hash) 或 None。"""
    m = re.match(r"^doc:([a-z0-9-]+):([^@]+)@([0-9a-f]{8,})$", source or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def url_of(sec: dict) -> str:
    return sec["url"] if sec["url"].startswith("http") else f"doc:{sec['book']}:{sec['sec_id']}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LEN]


# ---------- 抓取 ----------

def fetch(conn, book: str, limit=None, sec_id=None) -> list[str]:
    """抓取章节原文。缺省只抓 orig_text 为空的节；指定 --sec 强制重抓该节。
    zh 源抓完即为正文（text=orig_text 并落 hash）；en 源正文留待翻译。"""
    cfg = load_book(book)
    lines, fetched = [], 0
    rows = sections(conn, book)
    if sec_id is not None:
        rows = [s for s in rows if s["sec_id"] == str(sec_id)]
        if not rows:
            return [f"✗ {book} 没有节 {sec_id}（先 kg docs add）"]
    for sec in rows:
        if sec_id is None and sec["orig_text"]:
            continue
        if limit is not None and fetched >= limit:
            lines.append(f"（达到 --limit {limit}，剩余章节下次继续）")
            break
        try:
            text = _fetch_section_text(cfg, sec)
        except Exception as exc:  # 单节失败不杀整轮
            lines.append(f"✗ {sec['sec_id']} {sec['title']}: {exc}")
            fetched += 1
            continue
        if len(text) < MIN_SECTION_CHARS:
            lines.append(f"✗ {sec['sec_id']} {sec['title']}: 正文过短（{len(text)} 字），不入库")
            fetched += 1
            continue
        if cfg["lang"] == "zh":
            conn.execute(
                "UPDATE doc_sections SET orig_text=?, text=?, content_hash=?, fetched_at=?"
                " WHERE id=?", (text, text, _hash(text), time.time(), sec["id"]))
        else:
            # 重抓会使旧译文与原文脱节，清掉译文（hash 一并失效）
            conn.execute(
                "UPDATE doc_sections SET orig_text=?, text='', content_hash='',"
                " fetched_at=?, translated_at=NULL WHERE id=?",
                (text, time.time(), sec["id"]))
        conn.commit()
        fetched += 1
        lines.append(f"✓ {sec['sec_id']} {sec['title']}（{len(text)} 字）")
        time.sleep(1.0)  # 对源站礼貌限速
    return lines or ["没有需要抓取的章节"]


def _fetch_section_text(cfg: dict, sec: dict) -> str:
    locator = sec["url"]
    if locator.startswith("path:"):
        path = os.path.join(SOURCES_DIR, "..", locator[5:])
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    if cfg["type"] == "pdf":
        m = re.fullmatch(r"pages:(\d+)-(\d+)", locator)
        if not m:
            raise ValueError(f"pdf 源定位串应为 pages:起-止: {locator}")
        return _pdf_pages(cfg, int(m.group(1)), int(m.group(2)))
    resp = requests.get(locator, headers=_HEADERS, timeout=60)
    resp.raise_for_status()
    if "charset" not in resp.headers.get("content-type", "").lower():
        resp.encoding = "utf-8"  # 站点未声明编码时 requests 默认 latin-1，中文会乱码
    return htmltext.extract(resp.text)


def _pdf_pages(cfg: dict, first: int, last: int) -> str:
    if not shutil.which("pdftotext"):
        raise RuntimeError("需要 pdftotext（sudo apt install poppler-utils），"
                           "或在配置里改用 path: 指向本地转好的文本")
    os.makedirs(PDF_DIR, exist_ok=True)
    pdf_path = os.path.join(PDF_DIR, f"{cfg['book']}.pdf")
    if not os.path.exists(pdf_path):
        resp = requests.get(cfg["pdf_url"], headers=_HEADERS, timeout=300)
        resp.raise_for_status()
        with open(pdf_path, "wb") as f:
            f.write(resp.content)
    out = subprocess.run(["pdftotext", "-f", str(first), "-l", str(last),
                          "-layout", pdf_path, "-"],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"pdftotext 失败: {out.stderr[:200]}")
    return out.stdout.strip()


# ---------- 翻译（英文源 -> 中文正文快照）----------

def translate_section(conn, sec: dict) -> dict:
    """整节原子翻译：全部块成功才写入 text+hash，失败整节保持未翻译。
    块之间并行（llm.pmap，全局并发上限内），任一块失败整节抛出。"""
    chunks = split_chunks(sec["orig_text"], TRANSLATE_CHUNK_CHARS)
    out = llm.pmap(lambda chunk: llm.chat(
        [{"role": "system", "content": TRANSLATE_PROMPT},
         {"role": "user", "content": chunk}],
        temperature=0.2, model=llm.TRANSLATE_MODEL), chunks)
    text = "\n\n".join(p.strip() for p in out).strip()
    conn.execute("UPDATE doc_sections SET text=?, content_hash=?, translated_at=? WHERE id=?",
                 (text, _hash(text), time.time(), sec["id"]))
    conn.commit()
    return get_section(conn, sec["book"], sec["sec_id"])


def split_chunks(text: str, limit: int) -> list[str]:
    """按空行切段后贪心聚合成 ≤limit 的块；单段超长再硬切。翻译与 ingest 分块共用。"""
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 2 > limit and cur:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def ensure_text(conn, sec: dict) -> dict:
    """拿到可提取的中文正文；en 源未翻译则就地翻译（lazy，用到哪节翻哪节）。"""
    if sec["text"]:
        return sec
    if not sec["orig_text"]:
        raise RuntimeError(f"{sec['book']} {sec['sec_id']} 还没抓取正文（kg docs fetch）")
    return translate_section(conn, sec)


def translate(conn, book: str, limit=None, sec_id=None) -> list[str]:
    """批量预翻译（可选，lazy 之外的预热通道）。"""
    lines, done = [], 0
    rows = sections(conn, book)
    if sec_id is not None:
        rows = [s for s in rows if s["sec_id"] == str(sec_id)]
    for sec in rows:
        if not sec["orig_text"] or sec["text"]:
            continue
        if limit is not None and done >= limit:
            lines.append(f"（达到 --limit {limit}，剩余章节下次继续）")
            break
        try:
            new = translate_section(conn, sec)
            lines.append(f"✓ {sec['sec_id']} {sec['title']}（译文 {len(new['text'])} 字，"
                         f"@{new['content_hash']}）")
        except (RuntimeError, requests.RequestException) as exc:
            lines.append(f"✗ {sec['sec_id']} {sec['title']}: {exc}")
        done += 1
    return lines or ["没有需要翻译的章节（zh 源无需翻译）"]


# ---------- 节 ↔ 概念映射（ingest 选节 + toc 先修信号）----------

def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def _title_core(title: str) -> str:
    """剥掉章节号前缀：「3.1 线性回归」->「线性回归」。"""
    return re.sub(r"^[0-9.．\s]+", "", title).strip()


def _name_in_text(name: str, text: str, text_norm: str) -> bool:
    """正文命中，带短名守卫：<3 字符不匹配；ASCII 名要求词边界（防 GRU 撞 congruent）。"""
    if len(name) < MIN_NAME_CHARS:
        return False
    if re.fullmatch(r"[\x00-\x7f]+", name):
        return bool(re.search(r"\b" + re.escape(name.lower()) + r"\b", text.lower()))
    return _norm(name) in text_norm


def _section_texts(conn, book=None) -> list[dict]:
    """有正文的节（中文正文优先，未翻译的 en 节退用原文做匹配），按 (book, ord) 升序。"""
    rows = []
    for sec in sections(conn, book):
        body = sec["text"] or sec["orig_text"]
        if body:
            sec["_body"] = body
            sec["_body_norm"] = _norm(body)
            rows.append(sec)
    return rows


def section_for_node(conn, node: dict, book=None):
    """给锚点节点找教材节：节标题命中优先（跨书取最先注册的），其次正文首次出现。"""
    names = [node["name"]] + node["aliases"]
    rows = _section_texts(conn, book)
    for sec in rows:
        core = _title_core(sec["title"])
        if any(corpus._similar(nm, core) >= TITLE_MATCH_THRESHOLD for nm in names):
            return sec
    for sec in rows:
        if any(_name_in_text(nm, sec["_body"], sec["_body_norm"]) for nm in names):
            return sec
    return None


def concept_positions(conn) -> dict:
    """{node_id: {book: 首次出现的 ord}}，toc 先修信号的数据源。

    两遍扫描（节按 book, ord 升序）：第一遍只认节标题命中（更可信），
    第二遍对没有标题命中的（节点, 书）补正文首次出现；短名守卫同 _name_in_text。"""
    rows = _section_texts(conn)
    nodes = [n for n in db.list_nodes(conn) if n["status"] != "rejected"]
    pos = {n["id"]: {} for n in nodes}
    for sec in rows:
        core = _title_core(sec["title"])
        for n in nodes:
            if sec["book"] in pos[n["id"]]:
                continue
            if any(corpus._similar(nm, core) >= TITLE_MATCH_THRESHOLD
                   for nm in [n["name"]] + n["aliases"]):
                pos[n["id"]][sec["book"]] = sec["ord"]
    for sec in rows:
        for n in nodes:
            if sec["book"] in pos[n["id"]]:
                continue
            if any(_name_in_text(nm, sec["_body"], sec["_body_norm"])
                   for nm in [n["name"]] + n["aliases"]):
                pos[n["id"]][sec["book"]] = sec["ord"]
    return {nid: books for nid, books in pos.items() if books}


def stats(conn) -> str:
    rows = sections(conn)
    if not rows:
        return "文档语料为空（kg docs add sources/<book>.yaml 然后 kg docs fetch）"
    lines = []
    for book in sorted({s["book"] for s in rows}):
        rs = [s for s in rows if s["book"] == book]
        fetched = sum(1 for s in rs if s["orig_text"])
        ready = sum(1 for s in rs if s["text"])
        lines.append(f"{book}: 章节 {len(rs)}，已抓取 {fetched}，中文正文就绪 {ready}")
    return "\n".join(lines)
