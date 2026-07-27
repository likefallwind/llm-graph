"""LLM 队列复核：模型结论留痕，不直接改实体主类型。"""
from __future__ import annotations

import json

from . import llm, store
from .ontology import registry


TYPE_REVIEW_POLICY_VERSION = "entity-type-review-1"


def _review_type_with_llm(row: dict) -> dict:
    prompt = f"""你在复核教育知识图谱中的实体类型冲突。

实体主类型判据（单值必填，按编号顺序判，命中即停）：
{registry().entity_type_contract()}

实体规范名：{row['canonical_name']}
实体定义：{row['definition']}
当前主类型：{row['primary_type']}
冲突观察：
{json.dumps(row['assertions'], ensure_ascii=False)}

判断当前主类型是否应保留。注意：
1. 只依据定义和逐字证据判，不要看名称的字面，也不要用你对该术语的额外记忆。
2. 模型结论只用于分级，不会直接改主类型；不确定就 ambiguous。

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


def record_human_verdict(conn, target_type: str, target_id: int, verdict: str, *,
                         reason: str, reviewer: str = "human") -> dict:
    """把人工裁决写回库——`human_review` 队列此前只有出口没有入口。

    `store.decide` 早就能改状态（`decided_by != 'shadow'` 那条分支），但只有
    `decision.shadow_claim` 在调它，而 shadow 分支按设计不改状态。于是队列
    产出了待审项，人判完却无处可写。

    拒绝的 claim 不物理删除，只置 `rejected`：`LOOKUP_EXCLUDED_STATUSES` 已经
    把它排除在检索之外，而留着行才能事后复现"当初为什么拒"。导出成 gold 负例
    是另一步，见 `scripts/export_human_labels.py`。
    """
    from .decision import POLICY_VERSION
    if verdict not in {"approve", "reject"}:
        raise ValueError(f"非法裁决: {verdict}（只接受 approve / reject）")
    if not reason.strip():
        raise ValueError("人工裁决必须给出理由——它是 gold 负例的判定依据")
    if target_type == "claim":
        target = store.get_claim(conn, target_id)
    elif target_type == "entity":
        target = store.get_entity(conn, target_id)
    else:
        raise ValueError(f"只支持 claim / entity，收到 {target_type}")
    if target is None:
        raise ValueError(f"{target_type} 不存在: {target_id}")

    decision = store.decide(
        conn, target_type, target_id, verdict,
        decided_by="human", policy_version=POLICY_VERSION,
        reason=f"{reason.strip()}（reviewer={reviewer}）")
    after = (store.get_claim(conn, target_id) if target_type == "claim"
             else store.get_entity(conn, target_id))
    return {"decision_id": decision.id, "target_type": target_type,
            "target_id": target_id, "verdict": verdict,
            "status": after.status}


def review_type_conflicts(conn, limit: int = 50) -> list[dict]:
    """当前算法版本下、与当前主类型仍冲突的类型断言。

    两道过滤都是为了不让队列塞进假货：

    冲突与否是**相对当前主类型的派生事实**，所以现算而不是读 `status`——断言是
    追加的观察史，不因人工 retype 而改写，retype 之后已经不冲突的不该再排队。

    断言还必须产自当前算法版本。换词表之后，旧版本记下的观察类型跟新主类型比对
    没有意义：v4 的 `method` 与 v5 的 `solution` 字面不同但说的是一回事，全算
    冲突只会淹掉真信号。这类断言等重抽产生新观察即可，不需要回填也不该回填。
    """
    from .pipeline import ALGORITHM_VERSION
    rows = conn.execute(
        "SELECT a.id assertion_id,a.entity_id,a.observed_type,a.reason,"
        " a.source_snapshot_id,o.excerpt,o.location,"
        " e.canonical_name,e.entity_type primary_type,e.definition"
        " FROM entity_type_assertions a"
        " JOIN entities e ON e.id=a.entity_id"
        " JOIN observations o ON o.id=a.observation_id"
        " JOIN runs r ON r.id=o.run_id"
        " WHERE a.status='conflict' AND a.observed_type!=e.entity_type"
        " AND r.algorithm_version=?"
        " ORDER BY a.entity_id,a.id"
        " LIMIT ?", (ALGORITHM_VERSION, max(1, limit))).fetchall()
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
