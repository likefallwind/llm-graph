"""关系专用 Evidence 验证与全图硬约束。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import llm, store
from .ontology import registry


ENTAILMENT_PROMPT = """你是有据 Claim 复核器。只能依据给出的 evidence，不得使用模型记忆。

Claim：{subject} -{relation}-> {object}
关系语义：{semantics}
方向规则：{direction_rule}
语义硬约束：{semantic_guard}
Evidence：
---
{excerpt}
---

判断这段 evidence 是否支持该关系的类型和方向，并严格遵守方向规则。

特别检查端点是否被截短：evidence 里说的宾语，和 Claim 写的宾语，是不是同一个
东西。正文说「专家系统是一种人工智能程序」，支持的是「专家系统 is_a 人工智能
程序」，**不支持**「专家系统 is_a 人工智能」——一类程序不是一个研究领域的种。
少了限定词就是另一句话，这种情况判 insufficient。
输出 JSON：
{output_schema}
"""

VALIDATOR_VERSION = "entailment-validator-1"
ENTAILMENT_PROMPT_VERSION = "entailment-judge-3"


@dataclass(frozen=True)
class Validation:
    outcome: str
    reasons: tuple[str, ...]
    evidence_ids: tuple[int, ...]
    independent_supports: int
    high_authority_supports: int
    evidence_reviews: tuple[tuple[int, int | None], ...] = ()


def create_entailment_run(conn) -> int:
    """每轮蕴含复核建一条 run，记下判定用的模型、prompt 与注册表版本。"""
    return store.create_run(
        conn, "entailment_verification", VALIDATOR_VERSION,
        model=llm.CHAT_MODEL, prompt_version=ENTAILMENT_PROMPT_VERSION,
        config={
            "relation_registry_version": registry().version,
            "relation_validator_versions": registry().validator_versions(),
        })


def stale_evidence_ids(conn, claim_id: int) -> set[int]:
    """当前判定不是本版 validator/prompt 产出的证据。

    包含三类：从未判过、判定早于留痕机制（run_id 为空）、判定来自旧版本。
    """
    rows = conn.execute(
        "SELECT e.id FROM evidence e"
        " LEFT JOIN entailment_reviews r ON r.id=e.current_entailment_review_id"
        " LEFT JOIN runs run ON run.id=r.run_id"
        " WHERE e.claim_id=? AND e.mechanically_valid=1"
        "   AND (e.current_entailment_review_id IS NULL OR run.id IS NULL"
        "        OR run.algorithm_version!=? OR run.prompt_version!=?)",
        (claim_id, VALIDATOR_VERSION, ENTAILMENT_PROMPT_VERSION)).fetchall()
    return {row["id"] for row in rows}


def _prompt_context(claim, subject, object_) -> dict:
    policy = registry().relation(claim.relation)
    if policy["symmetric"]:
        direction_rule = (
            "此关系是对称关系，交换 subject/object 不改变含义；"
            "不得仅因 evidence 中两个端点的出现顺序相反而判定 contradicts。")
    else:
        direction_rule = "此关系是有向关系，必须检查 subject/object 方向。"
    if claim.relation == "part_of":
        semantic_guard = (
            "part_of 只能在 evidence 明确表达 subject 是 object 的真实结构部件，"
            "或 object 流程中明确列出的阶段时成立。用于、依赖、参与、帮助构建、"
            "产生、输入/输出、属性、子类型、先后学习均不是 part_of。"
            "请做反事实检查：移除 subject 后，object 是否缺少一个正文明确承认的"
            "组成部分或阶段；仅仅改变构建方式或用途不算。")
        output_schema = (
            '{"verdict":"supports|contradicts|insufficient",'
            '"composition_explicit":true|false,"reason":"一句话理由"}')
    else:
        semantic_guard = "按关系定义判断，不得把相邻概念强行归入该关系。"
        output_schema = (
            '{"verdict":"supports|contradicts|insufficient",'
            '"reason":"一句话理由"}')
    return {
        "subject": subject.canonical_name, "relation": claim.relation,
        "object": object_.canonical_name, "semantics": policy["description"],
        "direction_rule": direction_rule, "semantic_guard": semantic_guard,
        "output_schema": output_schema,
    }


def _judge_batch(prompts: list[str]) -> list:
    """并行调 M3 判断题；单项失败返回异常对象，不杀整批。"""
    def call(prompt):
        try:
            return llm.chat_json([{"role": "user", "content": prompt}])
        except (RuntimeError, ValueError) as exc:
            return exc
    return llm.pmap(call, prompts)


def _interpret(answer: dict, relation: str) -> tuple[str, str]:
    verdict = str(answer.get("verdict", "insufficient")).strip()
    if verdict not in {"supports", "contradicts", "insufficient"}:
        verdict = "insufficient"
    reason = str(answer.get("reason", ""))[:300]
    if (relation == "part_of" and verdict == "supports"
            and answer.get("composition_explicit") is not True):
        verdict = "insufficient"
        reason = (
            "未明确确认真实组成关系；"
            + (reason or "模型未返回 composition_explicit=true")
        )[:300]
    return verdict, reason


def verify_entailment_batch(conn, claim_ids, *, force: bool = False,
                            only_stale: bool = False,
                            run_id: int | None = None) -> list[str]:
    """跨 claim 批量复核蕴含。

    分三段：串行读库拼 prompt、并行调 LLM、串行落库。并发只发生在 HTTP 请求上，
    数据库始终单线程写——``llm.pmap`` 的 fn 不得触碰 sqlite 连接。
    """
    targets = []
    for claim_id in claim_ids:
        claim = store.get_claim(conn, claim_id)
        if not claim:
            raise ValueError(f"Claim 不存在: {claim_id}")
        context = _prompt_context(
            claim, store.get_entity(conn, claim.subject_id),
            store.get_entity(conn, claim.object_id))
        stale = stale_evidence_ids(conn, claim_id) if only_stale else set()
        for evidence in store.evidence_for_claim(conn, claim_id):
            if not evidence.mechanically_valid:
                continue
            if only_stale:
                if evidence.id not in stale:
                    continue
            elif not force and evidence.entailment != "unreviewed":
                continue
            targets.append((claim.relation, evidence, ENTAILMENT_PROMPT.format(
                excerpt=evidence.excerpt, **context)))
    if not targets:
        return []

    own_run = run_id is None
    if own_run:
        run_id = create_entailment_run(conn)
    answers = _judge_batch([prompt for _, _, prompt in targets])

    lines = []
    for (relation, evidence, _), answer in zip(targets, answers):
        if isinstance(answer, Exception) or not isinstance(answer, dict):
            detail = answer if isinstance(answer, Exception) else "返回非 JSON object"
            lines.append(f"evidence {evidence.id}: 复核失败（{detail}）")
            continue
        verdict, reason = _interpret(answer, relation)
        store.add_entailment_review(
            conn, evidence.id, verdict, run_id=run_id, reason=reason,
            raw_output=answer)
        lines.append(f"evidence {evidence.id}: {verdict}（{reason}）")
    if own_run:
        store.finish_run(conn, run_id, "completed")
    return lines


def verify_entailment(conn, claim_id: int, *, force: bool = False,
                      only_stale: bool = False, run_id: int | None = None) -> list[str]:
    return verify_entailment_batch(
        conn, [claim_id], force=force, only_stale=only_stale, run_id=run_id)


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


def _required_independent(policy: dict) -> int:
    """该关系要求的独立来源组数。

    两个键是同一个门槛的不同叫法（教学类关系用 curriculum）。用显式判空而不是
    ``or`` 串联：配成 0 时 ``or`` 会静默滑到下一个默认值，那是配置被忽略，
    不是配置生效。
    """
    minimum = policy.get("minimum_evidence", {})
    for key in ("independent_standard_sources", "independent_curriculum_sources"):
        if key in minimum:
            return int(minimum[key])
    return 2


def evaluate(conn, claim_id: int) -> Validation:
    claim = store.get_claim(conn, claim_id)
    if not claim:
        raise ValueError(f"Claim 不存在: {claim_id}")
    subject = store.get_entity(conn, claim.subject_id)
    object_ = store.get_entity(conn, claim.object_id)
    registry().validate_claim(subject.entity_type, claim.relation, object_.entity_type)
    policy = registry().relation(claim.relation)

    rows = conn.execute(
        "SELECT e.id,e.evidence_type,e.entailment,e.current_entailment_review_id,"
        " s.independence_group,s.authority_profile"
        " FROM evidence e"
        " JOIN source_snapshots ss ON ss.id=e.source_snapshot_id"
        " JOIN sources s ON s.id=ss.source_id"
        " WHERE e.claim_id=? AND e.mechanically_valid=1 ORDER BY e.id",
        (claim_id,)).fetchall()
    evidence_ids = tuple(row["id"] for row in rows)
    # 裁决依据的是「这些证据的这一次判定」，重判之后旧裁决才复现得出来。
    reviews = tuple(
        (row["id"], row["current_entailment_review_id"]) for row in rows)

    # 两层过滤，管的是两件不同的事：
    #   1. 全局 strength —— 这段文字是不是一句断言。目录序/超链接/共现是编排，
    #      既支持不了也反驳不了，两边都不计。原来只在支持侧过滤，一条共现的
    #      contradicts 却能把 claim 判去人工，那是不对称的。
    #   2. 关系白名单 accepted_evidence_types —— 这类断言能不能**建立**该关系。
    #      只过滤支持侧。建立不了不等于反驳不了。
    strong_supports = 0
    opposes = 0
    groups: set[str] = set()
    high = 0
    non_assertive: set[str] = set()
    unaccepted_supports: set[str] = set()
    for row in rows:
        verdict = row["entailment"]
        if verdict not in {"supports", "contradicts"}:
            continue
        evidence_type = row["evidence_type"]
        if not registry().is_assertive_evidence(evidence_type):
            non_assertive.add(evidence_type)
            continue
        if verdict == "contradicts":
            opposes += 1
            continue
        if not registry().is_strong_evidence(claim.relation, evidence_type):
            unaccepted_supports.add(evidence_type)
            continue
        strong_supports += 1
        groups.add(row["independence_group"])
        profile = json.loads(row["authority_profile"])
        level = (profile.get(claim.relation)
                 or profile.get("relations", {}).get(claim.relation))
        if level == "high":
            high += 1

    reasons: list[str] = []
    if non_assertive:
        reasons.append(
            f"有证据只是编排而非断言（{'、'.join(sorted(non_assertive))}），"
            "支持与反对均不计入")
    if unaccepted_supports:
        reasons.append(
            f"有支持证据的类型不被 {claim.relation} 接受"
            f"（{'、'.join(sorted(unaccepted_supports))}），不计入门槛")

    if _would_cycle(conn, claim):
        return Validation(
            "human_review", ("批准会引入无环关系环路",),
            evidence_ids, 0, 0, reviews)
    if opposes:
        reasons.insert(0, f"存在 {opposes} 条反对证据")
        return Validation(
            "human_review", tuple(reasons), evidence_ids, 0, 0, reviews)
    if strong_supports == 0:
        reasons.insert(
            0, "现有支持都不足以建立该关系" if unaccepted_supports or non_assertive
            else "没有通过蕴含验证的支持证据")
        return Validation(
            "needs_more_evidence", tuple(reasons), evidence_ids, 0, 0, reviews)

    independent = len(groups)
    required_independent = _required_independent(policy)
    required_high = policy.get("minimum_evidence", {}).get(
        "explicit_high_authority", 1)
    if high >= required_high and independent >= required_independent:
        reasons.append(
            f"强证据 {strong_supports} 条，独立来源组 {independent} 个，"
            f"高权威支持 {high} 条")
        outcome = "human_review" if policy.get("high_impact_review") else "auto_approve"
        if policy.get("high_impact_review"):
            reasons.append("高影响关系在校准完成前保留人工审核")
    else:
        reasons.append(
            f"证据未达门槛：独立来源组 {independent}/{required_independent}，"
            f"高权威支持 {high}/{required_high}")
        outcome = "needs_more_evidence"
    return Validation(
        outcome, tuple(reasons), evidence_ids, independent, high, reviews)
