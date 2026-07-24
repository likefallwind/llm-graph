"""语料驱动的新知识流水线：Snapshot -> Observation -> Claim -> Shadow。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import claims, coverage, decision, observations, store, validators


ALGORITHM_VERSION = "grounded-pipeline-1"


def read_file(conn, path: str, *, source_slug: str, source_name: str,
              source_type: str, independence_group: str, topic: str,
              version: str = "", language: str = "",
              authority_profile: dict | None = None,
              observations_path: str | None = None,
              max_entities: int = 20, max_claims: int = 30,
              verify_llm: bool = True) -> dict:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    return read_text(
        conn, text, source_slug=source_slug, source_name=source_name,
        source_type=source_type, independence_group=independence_group,
        topic=topic, version=version, language=language,
        authority_profile=authority_profile, observations_path=observations_path,
        max_entities=max_entities, max_claims=max_claims,
        verify_llm=verify_llm, uri=str(file_path),
        metadata={"local_path": str(file_path)})


def read_text(conn, text: str, *, source_slug: str, source_name: str,
              source_type: str, independence_group: str, topic: str,
              version: str = "", language: str = "",
              authority_profile: dict | None = None,
              observations_path: str | None = None,
              max_entities: int = 20, max_claims: int = 30,
              verify_llm: bool = True, uri: str = "",
              storage_ref: str = "", metadata: dict | None = None) -> dict:
    if not coverage.exists(conn, topic):
        raise ValueError(f"未知 coverage topic: {topic}")
    if not text.strip():
        raise ValueError("语料正文为空")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_id = store.upsert_source(
        conn, source_slug, source_name, source_type,
        independence_group=independence_group,
        authority_profile=authority_profile or {},
        metadata=metadata or {})
    snapshot = store.add_source_snapshot(
        conn, source_id, version or digest[:12], content=text,
        content_hash=digest, uri=uri, original_language=language,
        storage_ref=storage_ref, metadata=metadata)
    run_id = store.create_run(
        conn, "extraction", ALGORITHM_VERSION,
        prompt_version="grounded-extract-1",
        config={"topic": topic, "source_snapshot_id": snapshot.id})
    try:
        if observations_path:
            payload = json.loads(Path(observations_path).read_text(encoding="utf-8"))
            batch = observations.parse_payload(payload, text)
        else:
            batch = observations.extract(
                text, topic, max_entities=max_entities, max_claims=max_claims)
        materialized = claims.materialize(
            conn, batch, source_snapshot_id=snapshot.id, run_id=run_id)
        entailment_lines = []
        if verify_llm:
            for claim_id in materialized.claim_ids:
                entailment_lines.extend(validators.verify_entailment(conn, claim_id))
        shadows = [decision.shadow_claim(conn, claim_id)
                   for claim_id in materialized.claim_ids]
        for target in batch.next_reading_targets:
            conn.execute(
                "INSERT INTO reading_tasks"
                " (coverage_topic_id,query,reason,priority,status,source_derived,created_at,updated_at)"
                " SELECT ?,?,?,0.0,'pending',1,unixepoch(),unixepoch()"
                " WHERE EXISTS (SELECT 1 FROM coverage_topics WHERE id=?)",
                (topic, target.query, target.reason, topic))
        conn.commit()
        store.finish_run(conn, run_id, "completed")
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_processed"
            " (source_snapshot_id,coverage_topic_id,algorithm_version,run_id,processed_at)"
            " VALUES (?,?,?,?,unixepoch())",
            (snapshot.id, topic, ALGORITHM_VERSION, run_id))
        conn.commit()
    except Exception:
        store.finish_run(conn, run_id, "failed")
        raise
    return {
        "run_id": run_id,
        "source_snapshot_id": snapshot.id,
        "entities": list(materialized.entity_ids),
        "claims": list(materialized.claim_ids),
        "rejected": list(materialized.rejected),
        "entailment": entailment_lines,
        "shadow_decisions": shadows,
        "next_reading_targets": [
            {"query": item.query, "reason": item.reason}
            for item in batch.next_reading_targets],
    }


TEXTBOOK_AUTHORITY = {
    name: "high" for name in (
        "is_a", "subfield_of", "part_of", "prerequisite_of",
        "often_confused_with", "pedagogical_contrast_with", "alternative_to",
        "used_for", "solves", "evaluated_by", "optimizes", "derived_from")
}


def read_doc_section(conn, book: str, sec_id: str, *, topic: str,
                     observations_path: str | None = None,
                     max_entities: int = 20, max_claims: int = 30,
                     verify_llm: bool = True) -> dict:
    from . import docs
    sec = docs.get_section(conn, book, sec_id)
    if not sec:
        raise ValueError(f"教材章节不存在: {book} §{sec_id}")
    sec = docs.ensure_text(conn, sec)
    cfg = docs.load_book(book)
    return read_text(
        conn, sec["text"], source_slug=f"doc-{book}",
        source_name=cfg["title"], source_type="textbook",
        independence_group=f"book:{book}", topic=topic,
        version=f"{sec_id}@{sec['content_hash']}", language="zh",
        authority_profile=TEXTBOOK_AUTHORITY,
        observations_path=observations_path,
        max_entities=max_entities, max_claims=max_claims,
        verify_llm=verify_llm, uri=docs.url_of(sec),
        storage_ref=f"doc_sections:{sec['id']}",
        metadata={
            "book": book, "sec_id": sec_id, "title": sec["title"],
            "original_language": sec["orig_lang"],
            "translated": sec["orig_lang"] != "zh",
        })


def read_wiki_page(conn, lang: str, title: str, *, topic: str,
                   observations_path: str | None = None,
                   max_entities: int = 20, max_claims: int = 30,
                   verify_llm: bool = True) -> dict:
    from . import corpus
    page = corpus.get_page(conn, lang, title)
    if not page:
        raise ValueError(f"本地 Wikipedia 语料页不存在: {lang}:{title}")
    return read_text(
        conn, page["text"], source_slug=f"wikipedia-{lang}",
        source_name=f"Wikipedia {lang}", source_type="encyclopedia",
        independence_group="wikipedia", topic=topic,
        version=f"{title}@{page['revision_id']}", language=lang,
        authority_profile={}, observations_path=observations_path,
        max_entities=max_entities, max_claims=max_claims,
        verify_llm=verify_llm, uri=corpus.url_of(page),
        storage_ref=f"corpus:{page['id']}",
        metadata={
            "page_id": page["page_id"], "title": page["title"],
            "revision_id": page["revision_id"],
        })


def batch(conn, *, topic: str, doc_limit: int = 1, wiki_limit: int = 1,
          max_entities: int = 20, max_claims: int = 30,
          verify_llm: bool = True) -> dict:
    """选未被当前算法处理的本地教材节和已映射 Wikipedia 页面，各跑一个小批次。"""
    if not coverage.exists(conn, topic):
        raise ValueError(f"未知 coverage topic: {topic}")
    docs_rows = conn.execute(
        "SELECT d.book,d.sec_id,d.title FROM doc_sections d"
        " WHERE d.text!='' AND NOT EXISTS ("
        "   SELECT 1 FROM source_snapshots ss"
        "   JOIN pipeline_processed pp ON pp.source_snapshot_id=ss.id"
        "   WHERE ss.storage_ref='doc_sections:' || d.id"
        "     AND pp.coverage_topic_id=? AND pp.algorithm_version=?"
        " ) ORDER BY d.book,d.ord LIMIT ?",
        (topic, ALGORITHM_VERSION, max(0, doc_limit))).fetchall()
    wiki_rows = conn.execute(
        "SELECT DISTINCT c.lang,c.title,n.id node_id FROM corpus c"
        " JOIN node_page np ON np.lang=c.lang AND np.page_id=c.page_id"
        " JOIN nodes n ON n.id=np.node_id"
        " WHERE n.status IN ('seed','approved') AND NOT EXISTS ("
        "   SELECT 1 FROM source_snapshots ss"
        "   JOIN pipeline_processed pp ON pp.source_snapshot_id=ss.id"
        "   WHERE ss.storage_ref='corpus:' || c.id"
        "     AND pp.coverage_topic_id=? AND pp.algorithm_version=?"
        " ) ORDER BY n.id LIMIT ?",
        (topic, ALGORITHM_VERSION, max(0, wiki_limit))).fetchall()
    results, failures = [], []
    for row in docs_rows:
        label = f"doc:{row['book']}:{row['sec_id']}"
        try:
            output = read_doc_section(
                conn, row["book"], row["sec_id"], topic=topic,
                max_entities=max_entities, max_claims=max_claims,
                verify_llm=verify_llm)
            results.append({"source": label, "result": output})
        except Exception as exc:
            failures.append({"source": label, "error": str(exc)})
    for row in wiki_rows:
        label = f"wiki:{row['lang']}:{row['title']}"
        try:
            output = read_wiki_page(
                conn, row["lang"], row["title"], topic=topic,
                max_entities=max_entities, max_claims=max_claims,
                verify_llm=verify_llm)
            results.append({"source": label, "result": output})
        except Exception as exc:
            failures.append({"source": label, "error": str(exc)})
    return {
        "topic": topic,
        "selected": {
            "docs": [f"{row['book']}:{row['sec_id']}" for row in docs_rows],
            "wiki": [f"{row['lang']}:{row['title']}" for row in wiki_rows],
        },
        "completed": results,
        "failed": failures,
    }


def status(conn) -> dict:
    tables = {
        "sources": "sources",
        "snapshots": "source_snapshots",
        "entities": "entities",
        "claims": "claims",
        "evidence": "evidence",
        "observations": "observations",
        "decisions": "decisions",
        "reading_tasks": "reading_tasks",
        "legacy_entities": "legacy_entity_map",
        "legacy_claims": "legacy_claim_map",
        "migration_issues": "migration_issues",
        "processed_sources": "pipeline_processed",
    }
    result = {}
    for key, table in tables.items():
        result[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result["claims_by_status"] = {
        row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) n FROM claims GROUP BY status")}
    result["aliases_by_status"] = {
        row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) n FROM aliases GROUP BY status")}
    result["entity_resolution"] = {
        "events": conn.execute(
            "SELECT COUNT(*) FROM entity_resolution_events").fetchone()[0],
        "type_conflicts": conn.execute(
            "SELECT COUNT(*) FROM entity_type_assertions"
            " WHERE status='conflict'").fetchone()[0],
        "alignment_candidates": {
            row["status"]: row["n"] for row in conn.execute(
                "SELECT status,COUNT(*) n FROM entity_alignment_candidates"
                " GROUP BY status")
        },
    }
    result["latest_shadow"] = [
        dict(row) for row in conn.execute(
            "SELECT target_id,outcome,reason,created_at FROM decisions"
            " WHERE decided_by='shadow' ORDER BY id DESC LIMIT 10")]
    result["alignment_review"] = [
        dict(row) for row in conn.execute(
            "SELECT ac.id,ac.observed_name,e.canonical_name target,"
            " ac.score,ac.evidence_count,ac.independent_sources"
            " FROM entity_alignment_candidates ac"
            " JOIN entities e ON e.id=ac.entity_id"
            " WHERE ac.status='suspected'"
            " ORDER BY ac.score DESC,ac.id LIMIT 10")]
    return result
