"""从语料快照产生并校验结构化 Observation。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .ontology import registry


EXTRACT_PROMPT = """你是有据抽取器。只允许依据给出的语料，不得使用模型记忆补充知识。

覆盖主题：{topic}
允许的实体类型：{entity_types}
允许的关系：{relations}

要求：
1. 每个实体和 Claim 都必须附语料中的逐字摘录 evidence。
2. 每个 Claim 的 subject 和 object 必须同时出现在 entities 中。
3. 不确定时不输出；不得把超链接、共现或章节顺序直接当成类型化关系。
4. evidence_type 只能描述证据实际表达的类型，不得夸大。
5. 最多 {max_entities} 个实体、{max_claims} 个 Claim。

输出 JSON：
{{
  "entities": [
    {{
      "name": "规范名称",
      "entity_type": "允许的类型",
      "definition": "仅按正文概括",
      "aliases": [],
      "evidence": "逐字摘录",
      "location": "章节或段落说明"
    }}
  ],
  "claims": [
    {{
      "subject": "entities 中的名称",
      "relation": "允许的关系",
      "object": "entities 中的名称",
      "qualifiers": {{}},
      "evidence_type": "explicit_definition|explicit_taxonomy|explicit_composition|explicit_function|explicit_prerequisite|explicit_comparison|explicit_derivation|toc_order|hyperlink|cooccurrence",
      "evidence": "逐字摘录",
      "location": "章节或段落说明"
    }}
  ],
  "next_reading_targets": [
    {{"query": "语料中明确出现的术语或引用", "reason": "为什么值得继续读"}}
  ]
}}

语料：
---
{text}
---"""


@dataclass(frozen=True)
class EntityObservation:
    name: str
    entity_type: str
    definition: str
    aliases: tuple[str, ...]
    evidence: str
    location: str
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ClaimObservation:
    subject: str
    relation: str
    object: str
    qualifiers: dict[str, Any]
    evidence_type: str
    evidence: str
    location: str
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ReadingTarget:
    query: str
    reason: str


@dataclass(frozen=True)
class ObservationBatch:
    entities: tuple[EntityObservation, ...]
    claims: tuple[ClaimObservation, ...]
    next_reading_targets: tuple[ReadingTarget, ...]
    rejected: tuple[str, ...]


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def evidence_in_text(excerpt: str, text: str) -> bool:
    """逐字证据机械校验；仅容忍空白差异和省略号分段。"""
    if not excerpt or not text:
        return False
    normalized = _norm(text)
    parts = [_norm(p) for p in re.split(r"(?:\.{3}|…+)", excerpt) if _norm(p)]
    return bool(parts) and all(part in normalized for part in parts)


def parse_payload(payload: dict, source_text: str) -> ObservationBatch:
    reg = registry()
    entities: list[EntityObservation] = []
    claims: list[ClaimObservation] = []
    targets: list[ReadingTarget] = []
    rejected: list[str] = []
    entity_names: set[str] = set()

    for index, raw in enumerate(payload.get("entities", [])):
        name = str(raw.get("name", "")).strip()
        entity_type = str(raw.get("entity_type", "")).strip()
        evidence = str(raw.get("evidence", "")).strip()
        try:
            reg.validate_entity_type(entity_type)
        except ValueError as exc:
            rejected.append(f"entity[{index}] {exc}")
            continue
        if not name or not evidence_in_text(evidence, source_text):
            rejected.append(f"entity[{index}] 名称为空或 evidence 无法在语料中定位")
            continue
        key = name.casefold()
        if key in entity_names:
            rejected.append(f"entity[{index}] 重复实体「{name}」")
            continue
        entity_names.add(key)
        entities.append(EntityObservation(
            name=name, entity_type=entity_type,
            definition=str(raw.get("definition", "")).strip(),
            aliases=tuple(str(a).strip() for a in raw.get("aliases", []) if str(a).strip()),
            evidence=evidence, location=str(raw.get("location", "")).strip(), raw=raw))

    entity_by_name = {item.name.casefold(): item for item in entities}
    for index, raw in enumerate(payload.get("claims", [])):
        subject = str(raw.get("subject", "")).strip()
        object_ = str(raw.get("object", "")).strip()
        relation = str(raw.get("relation", "")).strip()
        evidence = str(raw.get("evidence", "")).strip()
        left = entity_by_name.get(subject.casefold())
        right = entity_by_name.get(object_.casefold())
        if not left or not right:
            rejected.append(f"claim[{index}] 端点必须同时出现在有效 entities 中")
            continue
        try:
            reg.validate_claim(left.entity_type, relation, right.entity_type)
        except ValueError as exc:
            rejected.append(f"claim[{index}] {exc}")
            continue
        if not evidence_in_text(evidence, source_text):
            rejected.append(f"claim[{index}] evidence 无法在语料中定位")
            continue
        claims.append(ClaimObservation(
            subject=subject, relation=relation, object=object_,
            qualifiers=raw.get("qualifiers") if isinstance(raw.get("qualifiers"), dict) else {},
            evidence_type=str(raw.get("evidence_type", "cooccurrence")).strip(),
            evidence=evidence, location=str(raw.get("location", "")).strip(), raw=raw))

    for raw in payload.get("next_reading_targets", []):
        query = str(raw.get("query", "")).strip()
        reason = str(raw.get("reason", "")).strip()
        if query and (_norm(query) in _norm(source_text)):
            targets.append(ReadingTarget(query=query, reason=reason))
        elif query:
            rejected.append(f"reading target「{query}」未在语料中明确出现")

    return ObservationBatch(
        entities=tuple(entities), claims=tuple(claims),
        next_reading_targets=tuple(targets), rejected=tuple(rejected))


def extract(source_text: str, topic: str, *, max_entities: int = 20,
            max_claims: int = 30) -> ObservationBatch:
    reg = registry()
    prompt = EXTRACT_PROMPT.format(
        topic=topic,
        entity_types="、".join(reg.entity_types),
        relations="、".join(reg.relations),
        max_entities=max_entities, max_claims=max_claims, text=source_text)
    payload = llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(payload, dict):
        raise ValueError("抽取器必须返回 JSON object")
    return parse_payload(payload, source_text)
