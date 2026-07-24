"""把验证结果记录为 Shadow Decision，不修改 Claim 状态。"""
from __future__ import annotations

from . import store, validators


POLICY_VERSION = "claim-policy-1"


def shadow_claim(conn, claim_id: int) -> dict:
    result = validators.evaluate(conn, claim_id)
    decision = store.decide(
        conn, "claim", claim_id, result.outcome,
        decided_by="shadow", policy_version=POLICY_VERSION,
        reason="；".join(result.reasons),
        evidence_ids=list(result.evidence_ids))
    return {
        "claim_id": claim_id,
        "decision_id": decision.id,
        "outcome": result.outcome,
        "reasons": list(result.reasons),
        "independent_supports": result.independent_supports,
        "high_authority_supports": result.high_authority_supports,
    }
