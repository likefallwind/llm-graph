"""LLM 队列复核：模型结论留痕，不直接改实体主类型。"""
from __future__ import annotations

import json

from . import llm, store
from .ontology import registry


TYPE_REVIEW_POLICY_VERSION = "entity-type-review-1"


def _review_type_with_llm(row: dict) -> dict:
    prompt = f"""你在复核教育知识图谱中的实体类型冲突。

允许类型：{json.dumps(list(registry().entity_types), ensure_ascii=False)}
实体规范名：{row['canonical_name']}
实体定义：{row['definition']}
当前主类型：{row['primary_type']}
冲突观察：
{json.dumps(row['assertions'], ensure_ascii=False)}

判断当前主类型是否应保留。注意：
1. concept 是兜底类型；明确的 field、task、method、model、architecture、loss 等更具体类型优先。
2. 不能仅凭实体名称判断，要结合定义和逐字证据。
3. 模型结论只用于分级，不会直接改主类型；不确定就 ambiguous。

只输出 JSON：
{{
  "verdict": "keep_primary|change_primary|ambiguous",
  "suggested_type": "允许类型之一",
  "confidence": 0.0,
  "reason": "简短理由"
}}"""
    payload = llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(payload, dict):
        raise ValueError("类型冲突复核器必须返回 JSON object")
    return payload


def review_type_conflicts(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT a.id assertion_id,a.entity_id,a.observed_type,a.reason,"
        " a.source_snapshot_id,o.excerpt,o.location,"
        " e.canonical_name,e.entity_type primary_type,e.definition"
        " FROM entity_type_assertions a"
        " JOIN entities e ON e.id=a.entity_id"
        " LEFT JOIN observations o ON o.id=a.observation_id"
        " WHERE a.status='conflict' ORDER BY a.entity_id,a.id"
        " LIMIT ?", (max(1, limit),)).fetchall()
    grouped: dict[int, dict] = {}
    for source_row in rows:
        row = dict(source_row)
        item = grouped.setdefault(row["entity_id"], {
            "entity_id": row["entity_id"],
            "canonical_name": row["canonical_name"],
            "definition": row["definition"],
            "primary_type": row["primary_type"],
            "assertions": [],
        })
        item["assertions"].append({
            "assertion_id": row["assertion_id"],
            "observed_type": row["observed_type"],
            "source_snapshot_id": row["source_snapshot_id"],
            "excerpt": row["excerpt"] or "",
            "location": row["location"] or "",
        })
    items = list(grouped.values())

    def classify(row):
        try:
            return _review_type_with_llm(row)
        except Exception as exc:
            return {
                "verdict": "error", "suggested_type": row["primary_type"],
                "confidence": 0.0, "reason": f"LLM 类型复核失败：{exc}",
            }

    outputs = llm.pmap(classify, items)
    results = []
    allowed_types = set(registry().entity_types)
    for row, output in zip(items, outputs):
        verdict = str(output.get("verdict", "ambiguous")).strip()
        if verdict not in {"keep_primary", "change_primary", "ambiguous", "error"}:
            verdict = "ambiguous"
        suggested_type = str(
            output.get("suggested_type", row["primary_type"])).strip()
        if suggested_type not in allowed_types:
            suggested_type = row["primary_type"]
            verdict = "ambiguous"
        try:
            confidence = min(1.0, max(0.0, float(output.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(output.get("reason", "")).strip()
        assertion_ids = [item["assertion_id"] for item in row["assertions"]]
        for assertion_id in assertion_ids:
            store.add_model_queue_review(
                conn, queue_type="type_conflict", item_id=assertion_id,
                model=llm.CHAT_MODEL, verdict=verdict, confidence=confidence,
                reason=reason, payload={
                    "entity_id": row["entity_id"],
                    "primary_type": row["primary_type"],
                    "suggested_type": suggested_type,
                }, policy_version=TYPE_REVIEW_POLICY_VERSION)
        results.append({
            "entity_id": row["entity_id"],
            "entity": row["canonical_name"],
            "primary_type": row["primary_type"],
            "observed_types": sorted({
                item["observed_type"] for item in row["assertions"]}),
            "assertion_ids": assertion_ids,
            "verdict": verdict,
            "suggested_type": suggested_type,
            "confidence": confidence,
            "auto_changed": False,
            "reason": reason,
        })
    return results
