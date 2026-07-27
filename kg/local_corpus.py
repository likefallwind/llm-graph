"""本地语料的统一访问：段落枚举与快照登记。

targeting 的定向检索和 alias_evidence 的别名声明扫描都要遍历同一批本地语料，
并且都需要把命中的段落登记成 source_snapshot 才能参与独立来源组计数。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import store


TEXTBOOK_AUTHORITY = {
    name: "high" for name in ("is_a", "part_of", "prerequisite_of")
}


@dataclass(frozen=True)
class LocalPassage:
    ref: str
    kind: str
    key: str
    text: str
    independence_group: str


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def passages(conn) -> list[LocalPassage]:
    """枚举本地语料段落。

    doc_sections / corpus 属于采集层（db.SCHEMA），新核心不硬依赖它们存在：
    只装了新 schema 的库应当得到空列表，而不是抛表不存在。
    """
    items = []
    if _has_table(conn, "doc_sections"):
        for row in conn.execute(
                "SELECT book,sec_id,text FROM doc_sections WHERE text!=''"):
            items.append(LocalPassage(
                ref=f"doc:{row['book']}:{row['sec_id']}", kind="doc",
                key=f"{row['book']}|{row['sec_id']}", text=row["text"],
                independence_group=f"book:{row['book']}"))
    if _has_table(conn, "corpus"):
        for row in conn.execute(
                "SELECT lang,title,text FROM corpus WHERE text!=''"):
            items.append(LocalPassage(
                ref=f"wiki:{row['lang']}:{row['title']}", kind="wiki",
                key=f"{row['lang']}|{row['title']}", text=row["text"],
                independence_group="wikipedia"))
    return items


def snapshot_for(conn, kind: str, key: str, text: str):
    """复用该段落已有的快照；content_hash 相同不会重复建。"""
    if kind == "doc":
        from . import docs
        book, sec_id = key.split("|", 1)
        section = docs.get_section(conn, book, sec_id)
        cfg = docs.load_book(book)
        source_id = store.upsert_source(
            conn, f"doc-{book}", cfg["title"], "textbook",
            independence_group=f"book:{book}",
            authority_profile=TEXTBOOK_AUTHORITY)
        return store.add_source_snapshot(
            conn, source_id, f"{sec_id}@{section['content_hash']}",
            content=text, uri=docs.url_of(section), original_language="zh",
            storage_ref=f"doc_sections:{section['id']}")
    from . import corpus
    lang, title = key.split("|", 1)
    page = corpus.get_page(conn, lang, title)
    source_id = store.upsert_source(
        conn, f"wikipedia-{lang}", f"Wikipedia {lang}", "encyclopedia",
        independence_group="wikipedia", authority_profile={})
    return store.add_source_snapshot(
        conn, source_id, f"{title}@{page['revision_id']}", content=text,
        uri=corpus.url_of(page), original_language=lang,
        storage_ref=f"corpus:{page['id']}")
