"""实体对齐：确定性精确匹配优先，LLM 只处理未命中与歧义。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable

from . import llm, store
from .observations import EntityObservation


RESOLVER_VERSION = "entity-resolver-5"
ALIGNMENT_POLICY_VERSION = "entity-alignment-policy-3"
LLM_SUSPECT_CONFIDENCE = 0.80
LLM_AUTO_LINK_CONFIDENCE = 0.95
MAX_CANDIDATES = 5
SAFE_DIRECT_MATCH_TYPES = {"translation_alias", "name_variant"}
MATCH_TYPES = {
    "translation_alias", "name_variant", "abbreviation", "symbol",
    "composite", "semantic_alias", "none",
}
NAME_VARIANT_SUFFIXES = (
    "问题", "方法", "算法", "模型", "函数", "任务", "估计",
    "problem", "method", "algorithm", "model", "function", "task",
    "estimation",
)


@dataclass(frozen=True)
class Resolution:
    entity_id: int | None
    outcome: str
    reason: str
    matched_by: str = ""
    normalized_name: str = ""
    candidate_ids: tuple[int, ...] = ()
    confidence: float | None = None
    selected_candidate_id: int | None = None


LLMNormalizer = Callable[[EntityObservation, list[dict]], dict]


def _compact_name(value: str) -> str:
    return re.sub(r"[\s`'\"_\-—–·()（）]+", "", value.casefold())


def _name_variant_roots(value: str) -> set[str]:
    roots = {_compact_name(value)}
    changed = True
    while changed:
        changed = False
        for root in tuple(roots):
            for suffix in NAME_VARIANT_SUFFIXES:
                compact_suffix = _compact_name(suffix)
                if root.endswith(compact_suffix) and len(root) > len(compact_suffix):
                    shorter = root[:-len(compact_suffix)]
                    if shorter not in roots:
                        roots.add(shorter)
                        changed = True
    return roots


def _has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _validated_direct_match_type(alias: str, canonical: str,
                                 claimed_type: str) -> str | None:
    """LLM 只能提议快捷类型；最终资格由确定性字符串规则确认。"""
    if _name_variant_roots(alias) & _name_variant_roots(canonical):
        return "name_variant"
    if (claimed_type == "translation_alias"
            and _has_cjk(alias) != _has_cjk(canonical)):
        return "translation_alias"
    return None


def _candidate_rows(conn, name: str, limit: int = MAX_CANDIDATES) -> list[dict]:
    """为 LLM 生成少量候选；相似度只召回，绝不直接决定合并。"""
    query = store.normalize_name(name)
    rows = conn.execute(
        "SELECT e.id,e.canonical_name,e.normalized_name,e.entity_type,e.definition,"
        " a.name alias_name,a.normalized_name alias_normalized,a.status alias_status"
        " FROM entities e LEFT JOIN aliases a"
        " ON a.entity_id=e.id AND a.status!='rejected'"
        " WHERE e.status!='rejected'").fetchall()
    by_entity: dict[int, dict] = {}
    for row in rows:
        candidate = by_entity.setdefault(row["id"], {
            "id": row["id"],
            "canonical_name": row["canonical_name"],
            "entity_type": row["entity_type"],
            "definition": row["definition"],
            "matched_names": [],
            "score": SequenceMatcher(None, query, row["normalized_name"]).ratio(),
        })
        if row["alias_normalized"]:
            score = SequenceMatcher(None, query, row["alias_normalized"]).ratio()
            candidate["score"] = max(candidate["score"], score)
            candidate["matched_names"].append({
                "name": row["alias_name"],
                "status": row["alias_status"],
            })
    ranked = sorted(
        by_entity.values(), key=lambda item: (-item["score"], item["id"]))
    return ranked[:limit]


def _llm_normalize(observation: EntityObservation,
                   candidates: list[dict]) -> dict:
    prompt = f"""你是知识图谱实体名称规范化与对齐器。

观察实体：
- 原始名称：{observation.name}
- 观察类型：{observation.entity_type}
- 定义：{observation.definition}
- 原文证据：{observation.evidence}

候选实体（字符串相似度仅用于召回，不代表相同）：
{json.dumps(candidates, ensure_ascii=False)}

规则：
1. 同一概念的缩写、译名、全称、常见别名可判为 existing。
2. 仅仅字符串相似、相关、上下位或同领域不能判为同一实体。
3. 类型不同是冲突信号，但不能单独推导为不同实体。
4. 不确定时必须输出 ambiguous。
5. canonical_name 是你建议的简洁规范名称；若选择 existing，candidate_id 必须来自候选列表。
6. 中英文直接互译且概念完全相同时，match_type=translation_alias。
7. 中文名称仅增加或省略“问题、方法、算法、模型、函数、任务”等词，
   且上下文含义没有变化时，match_type=name_variant。
8. 缩写、符号、组合概念、多义词分别标为 abbreviation、symbol、composite、
   semantic_alias；不得伪装成 translation_alias 或 name_variant。

只输出 JSON：
{{
  "decision": "existing|new|ambiguous",
  "candidate_id": null,
  "canonical_name": "规范名称",
  "proposed_alias": "原始名称或空字符串",
  "match_type": "translation_alias|name_variant|abbreviation|symbol|composite|semantic_alias|none",
  "confidence": 0.0,
  "reason": "简短理由"
}}"""
    payload = llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(payload, dict):
        raise ValueError("实体规范化器必须返回 JSON object")
    return payload


def _record(conn, observation: EntityObservation, result: Resolution, *,
            source_snapshot_id: int | None,
            observation_id: int | None) -> Resolution:
    store.add_resolution_event(
        conn, raw_name=observation.name,
        deterministic_name=store.normalize_name(observation.name),
        llm_normalized_name=result.normalized_name,
        entity_id=result.entity_id, outcome=result.outcome,
        selected_candidate_id=result.selected_candidate_id,
        matched_by=result.matched_by, candidate_ids=list(result.candidate_ids),
        confidence=result.confidence, reason=result.reason,
        resolver_version=RESOLVER_VERSION,
        source_snapshot_id=source_snapshot_id, observation_id=observation_id)
    if result.entity_id is not None:
        entity = store.get_entity(conn, result.entity_id)
        conflict = entity.entity_type != observation.entity_type
        store.add_type_assertion(
            conn, result.entity_id, observation.entity_type,
            source_snapshot_id=source_snapshot_id, observation_id=observation_id,
            status="conflict" if conflict else "consistent",
            reason=(
                f"实体主类型为 {entity.entity_type}，观察类型为 {observation.entity_type}"
                if conflict else "观察类型与实体主类型一致"))
    return result


def _matched_result(entity, observation: EntityObservation, *, matched_by: str,
                    reason: str, candidate_ids: tuple[int, ...] = (),
                    confidence: float | None = None) -> Resolution:
    conflict = entity.entity_type != observation.entity_type
    return Resolution(
        entity.id, "type_conflict" if conflict else "same_entity",
        (
            f"{reason}；已有实体类型 {entity.entity_type} 与观察类型"
            f" {observation.entity_type} 冲突，已记录类型断言"
            if conflict else reason
        ),
        matched_by=matched_by,
        normalized_name=entity.normalized_name,
        candidate_ids=candidate_ids,
        confidence=confidence,
        selected_candidate_id=entity.id if confidence is not None else None)


def resolve(conn, observation: EntityObservation, *,
            source_snapshot_id: int | None = None,
            observation_id: int | None = None,
            llm_normalizer: LLMNormalizer | None = None) -> Resolution:
    deterministic_name = store.normalize_name(observation.name)

    canonical = store.find_canonical_entity(conn, deterministic_name)
    if canonical:
        result = _matched_result(
            canonical, observation, matched_by="canonical_exact",
            reason="确定性规范化后精确命中 canonical name")
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    alias_hits = store.find_verified_alias_entities(conn, deterministic_name)
    if len(alias_hits) == 1:
        result = _matched_result(
            alias_hits[0], observation, matched_by="verified_alias_exact",
            reason="确定性规范化后精确命中 verified alias")
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    candidates = _candidate_rows(conn, deterministic_name)
    # verified alias 的多实体命中必须全部进入消歧候选，即使字符串候选上限较小。
    candidate_by_id = {item["id"]: item for item in candidates}
    for entity in alias_hits:
        candidate_by_id.setdefault(entity.id, {
            "id": entity.id,
            "canonical_name": entity.canonical_name,
            "entity_type": entity.entity_type,
            "definition": entity.definition,
            "matched_names": [{"name": observation.name, "status": "verified"}],
            "score": 1.0,
        })
    candidates = sorted(
        candidate_by_id.values(), key=lambda item: (-item["score"], item["id"]))
    candidate_ids = tuple(item["id"] for item in candidates)

    normalizer = llm_normalizer or _llm_normalize
    try:
        normalized = normalizer(observation, candidates)
    except Exception as exc:
        result = Resolution(
            None, "ambiguous", f"LLM 规范化失败：{exc}",
            matched_by="llm_error", normalized_name=deterministic_name,
            candidate_ids=candidate_ids)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    decision = str(normalized.get("decision", "")).strip()
    canonical_name = str(normalized.get("canonical_name", "")).strip()
    reason = str(normalized.get("reason", "")).strip() or "LLM 未提供理由"
    try:
        confidence = float(normalized.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    match_type = str(normalized.get("match_type", "semantic_alias")).strip()
    if match_type not in MATCH_TYPES:
        match_type = "semantic_alias"
    reason = f"[{match_type}] {reason}"

    # LLM 的规范名称也必须重新经过确定性精确检查。
    normalized_hit = (
        store.find_canonical_entity(conn, canonical_name) if canonical_name else None)
    selected = normalized_hit if decision == "existing" else None
    if decision == "existing" and not selected:
        try:
            selected_id = int(normalized.get("candidate_id"))
        except (TypeError, ValueError):
            selected_id = 0
        if selected_id in candidate_ids:
            selected = store.get_entity(conn, selected_id)

    if selected and confidence >= LLM_SUSPECT_CONFIDENCE:
        if selected.id not in candidate_ids:
            candidate_ids = (*candidate_ids, selected.id)
        direct_match_type = _validated_direct_match_type(
            observation.name, selected.canonical_name, match_type)
        safe_direct = (
            direct_match_type in SAFE_DIRECT_MATCH_TYPES
            and confidence >= LLM_AUTO_LINK_CONFIDENCE)
        if safe_direct:
            match_type = direct_match_type
        if deterministic_name != selected.normalized_name:
            store.add_alias(
                conn, selected.id, observation.name,
                source_snapshot_id=source_snapshot_id,
                status="verified" if safe_direct else "proposed",
                alias_type=match_type,
                evidence_excerpt=observation.evidence)
        alignment = store.add_alignment_evidence(
            conn, observed_name=observation.name, entity_id=selected.id,
            confidence=confidence, policy_version=ALIGNMENT_POLICY_VERSION,
            resolver_version=RESOLVER_VERSION, reason=reason,
            source_snapshot_id=source_snapshot_id,
            observation_id=observation_id, direct_verify=safe_direct)
        if safe_direct or alignment["status"] == "verified":
            matched_by = (
                f"llm_{match_type}"
                if safe_direct else (
                "accumulated_alignment"
                if alignment["status"] == "verified"
                and confidence < LLM_AUTO_LINK_CONFIDENCE
                else (
                    "llm_canonical_exact"
                    if normalized_hit else "llm_candidate")))
            result = _matched_result(
                selected, observation, matched_by=matched_by,
                reason=reason, candidate_ids=candidate_ids, confidence=confidence)
            return _record(
                conn, observation, result, source_snapshot_id=source_snapshot_id,
                observation_id=observation_id)
        result = Resolution(
            None, "suspected_same_entity",
            (
                f"{reason}；已记录疑似同实体，累计分 {alignment['score']:.3f}，"
                f"独立来源 {alignment['independent_sources']}/2"
            ),
            matched_by="llm_suspected",
            normalized_name=canonical_name or deterministic_name,
            candidate_ids=candidate_ids, confidence=confidence,
            selected_candidate_id=selected.id)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    if decision == "new" and canonical_name and confidence >= LLM_AUTO_LINK_CONFIDENCE:
        entity = store.add_entity(
            conn, canonical_name, observation.entity_type,
            definition=observation.definition, status="proposed",
            metadata={"created_from": "llm_normalized_grounded_observation"})
        if store.normalize_name(observation.name) != entity.normalized_name:
            store.add_alias(
                conn, entity.id, observation.name,
                source_snapshot_id=source_snapshot_id, status="proposed",
                evidence_excerpt=observation.evidence)
        result = Resolution(
            entity.id, "created", reason, matched_by="llm_new",
            normalized_name=entity.normalized_name, candidate_ids=candidate_ids,
            confidence=confidence)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    result = Resolution(
        None, "ambiguous",
        f"{reason}；置信度 {confidence:.2f} 未达到自动对齐阈值"
        if decision in {"existing", "new"} else reason,
        matched_by="llm_ambiguous", normalized_name=canonical_name or deterministic_name,
        candidate_ids=candidate_ids, confidence=confidence)
    return _record(
        conn, observation, result, source_snapshot_id=source_snapshot_id,
        observation_id=observation_id)


def review_proposed_aliases(conn, limit: int = 50) -> list[dict]:
    """用 LLM 批量复核现有 alias；仅安全直译/名称变体可自动验证。"""
    rows = conn.execute(
        "SELECT a.id alias_id,a.entity_id,a.name,a.evidence_excerpt,"
        " a.source_snapshot_id,e.canonical_name,e.entity_type,e.definition"
        " FROM aliases a JOIN entities e ON e.id=a.entity_id"
        " WHERE a.status='proposed' ORDER BY a.id LIMIT ?",
        (max(1, limit),)).fetchall()

    def classify(row):
        observation = EntityObservation(
            name=row["name"], entity_type=row["entity_type"],
            definition=row["definition"], aliases=(),
            evidence=row["evidence_excerpt"], location="alias review", raw={})
        candidate = [{
            "id": row["entity_id"], "canonical_name": row["canonical_name"],
            "entity_type": row["entity_type"], "definition": row["definition"],
            "matched_names": [], "score": 1.0,
        }]
        try:
            return _llm_normalize(observation, candidate)
        except Exception as exc:
            return {
                "decision": "error",
                "match_type": "none",
                "confidence": 0.0,
                "reason": f"LLM alias 复核失败：{exc}",
            }

    outputs = llm.pmap(classify, rows)
    results = []
    for row, output in zip(rows, outputs):
        decision = str(output.get("decision", "")).strip()
        match_type = str(output.get("match_type", "none")).strip()
        if match_type not in MATCH_TYPES:
            match_type = "none"
        try:
            confidence = min(1.0, max(0.0, float(output.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(output.get("reason", "")).strip()
        direct_match_type = _validated_direct_match_type(
            row["name"], row["canonical_name"], match_type)
        safe = (
            decision == "existing"
            and direct_match_type in SAFE_DIRECT_MATCH_TYPES
            and confidence >= LLM_AUTO_LINK_CONFIDENCE)
        if safe:
            match_type = direct_match_type
        rejected = (
            decision == "new"
            and match_type in {"composite", "none"}
            and confidence >= LLM_AUTO_LINK_CONFIDENCE)
        status = "verified" if safe else "rejected" if rejected else "proposed"
        store.update_alias_classification(
            conn, row["alias_id"], status=status, alias_type=match_type)
        if safe:
            store.add_alignment_evidence(
                conn, observed_name=row["name"], entity_id=row["entity_id"],
                confidence=confidence,
                policy_version=ALIGNMENT_POLICY_VERSION,
                resolver_version=RESOLVER_VERSION,
                reason=f"[{match_type}] {reason}",
                source_snapshot_id=row["source_snapshot_id"],
                direct_verify=True)
        results.append({
            "alias_id": row["alias_id"],
            "alias": row["name"],
            "entity": row["canonical_name"],
            "decision": decision,
            "match_type": match_type,
            "confidence": confidence,
            "status": status,
            "reason": reason,
        })
    return results
