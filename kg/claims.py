"""把有据 Observation 解析、消歧并聚合为 Entity/Claim/Evidence。"""
from __future__ import annotations

from dataclasses import dataclass

from . import entity_resolution, store
from .observations import ObservationBatch


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
