"""关系专用 Evidence 验证与全图硬约束。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import llm, store
from .ontology import registry


ENTAILMENT_PROMPT = """你是有据 Claim 复核器。只能依据给出的 evidence，不得使用模型记忆。

Claim：{subject} -{relation}-> {object}
关系语义：{semantics}
Evidence：
---
{excerpt}
---

判断这段 evidence 是否支持该关系的类型和方向。
输出 JSON：
{{"verdict":"supports|contradicts|insufficient","reason":"一句话理由"}}
"""

STRONG_TYPES = {
    "explicit_definition",
    "explicit_taxonomy",
    "explicit_composition",
    "explicit_function",
    "explicit_prerequisite",
    "explicit_comparison",
    "explicit_derivation",
    "structured_authoritative",
}


@dataclass(frozen=True)
class Validation:
    outcome: str
    reasons: tuple[str, ...]
    evidence_ids: tuple[int, ...]
    independent_supports: int
    high_authority_supports: int


def verify_entailment(conn, claim_id: int) -> list[str]:
    claim = store.get_claim(conn, claim_id)
    if not claim:
        raise ValueError(f"Claim 不存在: {claim_id}")
    subject = store.get_entity(conn, claim.subject_id)
    object_ = store.get_entity(conn, claim.object_id)
    semantics = registry().relation(claim.relation)["description"]
    lines = []
    for evidence in store.evidence_for_claim(conn, claim_id):
        if not evidence.mechanically_valid or evidence.entailment != "unreviewed":
            continue
        answer = llm.chat_json([{"role": "user", "content": ENTAILMENT_PROMPT.format(
            subject=subject.canonical_name, relation=claim.relation,
            object=object_.canonical_name, semantics=semantics,
            excerpt=evidence.excerpt)}])
        verdict = str(answer.get("verdict", "insufficient")).strip()
        if verdict not in {"supports", "contradicts", "insufficient"}:
            verdict = "insufficient"
        reason = str(answer.get("reason", ""))[:300]
        store.update_entailment(conn, evidence.id, verdict, reason=reason)
        lines.append(f"evidence {evidence.id}: {verdict}（{reason}）")
    return lines


def _would_cycle(conn, claim) -> bool:
    policy = registry().relation(claim.relation)
    if not policy["acyclic"]:
        return False
    adjacency: dict[int, set[int]] = {}
    rows = conn.execute(
        "SELECT subject_id,object_id FROM claims"
        " WHERE relation=? AND status='published'", (claim.relation,)).fetchall()
    for row in rows:
        adjacency.setdefault(row["subject_id"], set()).add(row["object_id"])
    stack, seen = [claim.object_id], set()
    while stack:
        current = stack.pop()
        if current == claim.subject_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, set()) - seen)
    return False


def evaluate(conn, claim_id: int) -> Validation:
    claim = store.get_claim(conn, claim_id)
    if not claim:
        raise ValueError(f"Claim 不存在: {claim_id}")
    subject = store.get_entity(conn, claim.subject_id)
    object_ = store.get_entity(conn, claim.object_id)
    registry().validate_claim(subject.entity_type, claim.relation, object_.entity_type)
    policy = registry().relation(claim.relation)
    evidence = store.evidence_for_claim(conn, claim_id)
    valid = [item for item in evidence if item.mechanically_valid]
    supports = [item for item in valid if item.entailment == "supports"]
    opposes = [item for item in valid if item.entailment == "contradicts"]
    reasons: list[str] = []

    if _would_cycle(conn, claim):
        return Validation(
            "human_review", ("批准会引入无环关系环路",),
            tuple(item.id for item in valid), 0, 0)
    if opposes:
        reasons.append(f"存在 {len(opposes)} 条反对证据")
        return Validation(
            "human_review", tuple(reasons),
            tuple(item.id for item in valid), 0, 0)
    if not supports:
        return Validation(
            "needs_more_evidence", ("没有通过蕴含验证的支持证据",),
            tuple(item.id for item in valid), 0, 0)

    rows = conn.execute(
        "SELECT e.id,e.evidence_type,s.independence_group,s.authority_profile"
        " FROM evidence e"
        " JOIN source_snapshots ss ON ss.id=e.source_snapshot_id"
        " JOIN sources s ON s.id=ss.source_id"
        " WHERE e.claim_id=? AND e.mechanically_valid=1 AND e.entailment='supports'",
        (claim_id,)).fetchall()
    groups = set()
    high = 0
    strong = 0
    for row in rows:
        groups.add(row["independence_group"])
        if row["evidence_type"] in STRONG_TYPES:
            strong += 1
        profile = json.loads(row["authority_profile"])
        level = profile.get(claim.relation) or profile.get("relations", {}).get(claim.relation)
        if level == "high" and row["evidence_type"] in STRONG_TYPES:
            high += 1

    independent = len(groups)
    minimum = policy.get("minimum_evidence", {})
    required_independent = (
        minimum.get("independent_standard_sources")
        or minimum.get("independent_curriculum_sources")
        or 2)
    required_high = minimum.get("explicit_high_authority", 1)
    if strong == 0:
        reasons.append("现有支持仅为弱证据")
        outcome = "needs_more_evidence"
    elif high >= required_high or independent >= required_independent:
        reasons.append(
            f"强证据 {strong} 条，独立来源组 {independent} 个，高权威支持 {high} 条")
        outcome = "human_review" if policy.get("high_impact_review") else "auto_approve"
        if policy.get("high_impact_review"):
            reasons.append("高影响关系在校准完成前保留人工审核")
    else:
        reasons.append(
            f"证据未达门槛：独立来源组 {independent}/{required_independent}，"
            f"高权威支持 {high}/{required_high}")
        outcome = "needs_more_evidence"
    return Validation(
        outcome, tuple(reasons), tuple(item.id for item in valid), independent, high)
