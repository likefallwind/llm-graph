"""知识核心的只读数据对象。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceSnapshot:
    id: int
    source_id: int
    version: str
    content_hash: str
    uri: str
    original_language: str
    content: str
    storage_ref: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Entity:
    id: int
    canonical_name: str
    normalized_name: str
    entity_type: str
    definition: str
    status: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Claim:
    id: int
    subject_id: int
    relation: str
    object_id: int
    qualifiers: dict[str, Any]
    status: str
    confidence: float | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Evidence:
    id: int
    entity_id: int | None
    claim_id: int | None
    source_snapshot_id: int
    polarity: str
    evidence_type: str
    excerpt: str
    location: str
    mechanically_valid: bool
    entailment: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Decision:
    id: int
    target_type: str
    target_id: int
    outcome: str
    decided_by: str
    policy_version: str
    reason: str
    evidence_snapshot: list[int]
    batch_id: str
