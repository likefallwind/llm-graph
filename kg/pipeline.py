"""语料驱动的新知识流水线：Snapshot -> Observation -> Claim -> Shadow。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import claims, decision, observations, store, validators


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
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_id = store.upsert_source(
        conn, source_slug, source_name, source_type,
        independence_group=independence_group,
        authority_profile=authority_profile or {},
        metadata={"local_path": str(file_path)})
    snapshot = store.add_source_snapshot(
        conn, source_id, version or digest[:12], content=text,
        content_hash=digest, uri=str(file_path), original_language=language)
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
    }
    result = {}
    for key, table in tables.items():
        result[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result["claims_by_status"] = {
        row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) n FROM claims GROUP BY status")}
    result["latest_shadow"] = [
        dict(row) for row in conn.execute(
            "SELECT target_id,outcome,reason,created_at FROM decisions"
            " WHERE decided_by='shadow' ORDER BY id DESC LIMIT 10")]
    return result
