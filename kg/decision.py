"""把验证结果记录为 Shadow Decision，不修改 Claim 状态。"""
from __future__ import annotations

from . import store, validators


# 4：证据计数改为两层——全局 strength 决定一段文字算不算断言（支持与反对同时
# 适用），关系白名单只决定它能不能建立该关系（只作用于支持侧）。
POLICY_VERSION = "claim-policy-5"


def shadow_claim(conn, claim_id: int) -> dict:
    result = validators.evaluate(conn, claim_id)
    decision = store.decide(
        conn, "claim", claim_id, result.outcome,
        decided_by="shadow", policy_version=POLICY_VERSION,
        reason="；".join(result.reasons),
        evidence_ids=list(result.evidence_ids),
        evidence_reviews=list(result.evidence_reviews))
    return {
        "claim_id": claim_id,
        "decision_id": decision.id,
        "outcome": result.outcome,
        "reasons": list(result.reasons),
        "independent_supports": result.independent_supports,
        "high_authority_supports": result.high_authority_supports,
    }
