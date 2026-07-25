"""把有据 Observation 解析、消歧并聚合为 Entity/Claim/Evidence。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import entity_resolution, store
from .observations import EntityObservation, ObservationBatch


@dataclass(frozen=True)
class MaterializationResult:
    entity_ids: tuple[int, ...]
    claim_ids: tuple[int, ...]
    rejected: tuple[str, ...]


def materialize(conn, batch: ObservationBatch, *, source_snapshot_id: int,
                run_id: int) -> MaterializationResult:
    resolved: dict[str, int] = {}
    entity_ids: list[int] = []
    claim_ids: list[int] = []
    rejected = list(batch.rejected)
    suspected: set[str] = set()

    for item in batch.entities:
        observation_id = store.add_observation(
            conn, run_id, source_snapshot_id,
            subject_text=item.name, subject_type=item.entity_type,
            excerpt=item.evidence, location=item.location, payload=item.raw)
        result = entity_resolution.resolve(
            conn, item, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)
        if result.entity_id is None:
            if result.outcome == "suspected_same_entity":
                suspected.add(item.name.casefold())
                rejected.append(f"实体「{item.name}」疑似对齐，等待更多证据：{result.reason}")
            else:
                store.resolve_observation(conn, observation_id, False)
                rejected.append(f"实体「{item.name}」消歧失败：{result.reason}")
            continue
        resolved[item.name.casefold()] = result.entity_id
        entity_ids.append(result.entity_id)
        store.add_evidence(
            conn, source_snapshot_id, item.evidence, "entity_description",
            entity_id=result.entity_id, location=item.location,
            mechanically_valid=True, extraction_run_id=run_id,
            metadata={"resolution": result.outcome, "resolution_reason": result.reason})
        for alias in item.aliases:
            try:
                store.add_alias(
                    conn, result.entity_id, alias,
                    source_snapshot_id=source_snapshot_id, status="proposed",
                    evidence_excerpt=item.evidence)
            except ValueError as exc:
                rejected.append(f"实体「{item.name}」别名「{alias}」未登记：{exc}")
        store.resolve_observation(conn, observation_id, True)

    for item in batch.claims:
        observation_id = store.add_observation(
            conn, run_id, source_snapshot_id,
            subject_text=item.subject, relation=item.relation,
            object_text=item.object, excerpt=item.evidence,
            location=item.location, payload=item.raw)
        subject_id = resolved.get(item.subject.casefold())
        object_id = resolved.get(item.object.casefold())
        if not subject_id or not object_id:
            if (item.subject.casefold() in suspected
                    or item.object.casefold() in suspected):
                rejected.append(
                    f"Claim「{item.subject} -{item.relation}-> {item.object}」"
                    "等待疑似实体对齐")
            else:
                store.resolve_observation(conn, observation_id, False)
                rejected.append(
                    f"Claim「{item.subject} -{item.relation}-> {item.object}」"
                    "端点未完成消歧")
            continue
        try:
            claim = store.add_claim(
                conn, subject_id, item.relation, object_id,
                qualifiers=item.qualifiers, status="proposed",
                metadata={"created_from": "grounded_observation"})
            store.add_evidence(
                conn, source_snapshot_id, item.evidence, item.evidence_type,
                claim_id=claim.id, location=item.location,
                mechanically_valid=True, extraction_run_id=run_id)
        except ValueError as exc:
            store.resolve_observation(conn, observation_id, False)
            rejected.append(str(exc))
            continue
        claim_ids.append(claim.id)
        store.resolve_observation(conn, observation_id, True)

    return MaterializationResult(
        entity_ids=tuple(dict.fromkeys(entity_ids)),
        claim_ids=tuple(dict.fromkeys(claim_ids)),
        rejected=tuple(rejected))


def _exact_entity_id(conn, name: str) -> int | None:
    canonical = store.find_canonical_entity(conn, name)
    if canonical:
        return canonical.id
    aliases = store.find_verified_alias_entities(conn, name)
    return aliases[0].id if len(aliases) == 1 else None


def replay_pending(conn, limit: int = 100) -> dict:
    """幂等重放 pending Observation：先实体，后 Claim。"""
    rows = conn.execute(
        "SELECT * FROM observations WHERE status='pending'"
        " ORDER BY CASE WHEN relation='' THEN 0 ELSE 1 END,id LIMIT ?",
        (max(1, limit),)).fetchall()
    resolved_entities: list[int] = []
    resolved_claims: list[int] = []
    rejected: list[dict] = []
    still_pending: list[int] = []

    for row in (item for item in rows if not item["relation"]):
        raw = json.loads(row["payload"])
        observation = EntityObservation(
            name=row["subject_text"],
            entity_type=row["subject_type"] or str(raw.get("entity_type", "")),
            definition=str(raw.get("definition", raw.get("定义", ""))).strip(),
            aliases=tuple(
                str(value).strip() for value in raw.get("aliases", [])
                if str(value).strip()),
            evidence=row["excerpt"], location=row["location"], raw=raw)
        result = entity_resolution.resolve(
            conn, observation, source_snapshot_id=row["source_snapshot_id"],
            observation_id=row["id"])
        if result.entity_id is None:
            still_pending.append(row["id"])
            continue
        store.add_evidence(
            conn, row["source_snapshot_id"], row["excerpt"],
            "entity_description", entity_id=result.entity_id,
            location=row["location"], mechanically_valid=True,
            extraction_run_id=row["run_id"],
            metadata={
                "resolution": result.outcome,
                "resolution_reason": result.reason,
                "replayed_observation_id": row["id"],
            })
        for alias in observation.aliases:
            try:
                store.add_alias(
                    conn, result.entity_id, alias,
                    source_snapshot_id=row["source_snapshot_id"],
                    status="proposed", evidence_excerpt=row["excerpt"])
            except ValueError:
                pass
        store.resolve_observation(conn, row["id"], True)
        resolved_entities.append(result.entity_id)

    for row in (item for item in rows if item["relation"]):
        subject_id = _exact_entity_id(conn, row["subject_text"])
        object_id = _exact_entity_id(conn, row["object_text"])
        if not subject_id or not object_id:
            still_pending.append(row["id"])
            continue
        raw = json.loads(row["payload"])
        try:
            claim = store.add_claim(
                conn, subject_id, row["relation"], object_id,
                qualifiers=raw.get("qualifiers", {}), status="proposed",
                metadata={
                    "created_from": "replayed_grounded_observation",
                    "replayed_observation_id": row["id"],
                })
            store.add_evidence(
                conn, row["source_snapshot_id"], row["excerpt"],
                str(raw.get("evidence_type", "cooccurrence")),
                claim_id=claim.id, location=row["location"],
                mechanically_valid=True, extraction_run_id=row["run_id"])
        except ValueError as exc:
            store.resolve_observation(conn, row["id"], False)
            rejected.append({"observation_id": row["id"], "reason": str(exc)})
            continue
        store.resolve_observation(conn, row["id"], True)
        resolved_claims.append(claim.id)

    return {
        "examined": len(rows),
        "resolved_entities": list(dict.fromkeys(resolved_entities)),
        "resolved_claims": list(dict.fromkeys(resolved_claims)),
        "rejected": rejected,
        "still_pending": list(dict.fromkeys(still_pending)),
    }
