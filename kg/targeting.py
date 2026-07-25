"""按 Claim 定向补证：先机械找候选段落，再中性抽取。

读整章碰运气的命中率很低——两本教材讲同一个概念，很少写出同一个三元组。
这里反过来：给定一条证据不足的 Claim，在本地语料里找同时提到两端概念的段落，
只对这些段落做抽取。检索全程零 LLM。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from . import llm, local_corpus, store, validators
from .observations import evidence_in_text
from .ontology import registry


TARGET_PROMPT = """下面是一段本地语料正文。请判断它是否明确陈述了「{left}」和「{right}」
这两个概念之间的关系。

只依据这段正文判断，不得使用模型记忆补充。要求：
1. 两个词只是出现在同一段，不等于存在关系；没有明确陈述就返回 relation="none"。
2. 方向由你根据正文判断，不要假设哪个是 subject。
3. evidence 必须是正文的逐字摘录。
4. evidence_type 只能描述证据实际表达的类型，不得夸大。

允许的关系及限定字段契约：{relations}

输出 JSON：
{{
  "relation": "关系名或 none",
  "subject": "{left} 或 {right}",
  "object": "另一个",
  "qualifiers": {{}},
  "evidence_type": "explicit_definition|explicit_taxonomy|explicit_composition|explicit_prerequisite|toc_order|hyperlink|cooccurrence",
  "evidence": "逐字摘录",
  "reason": "一句话理由"
}}

正文：
---
{passage}
---"""

WINDOW = 600
PROMPT_VERSION = "claim-targeted-1"
ALGORITHM_VERSION = "claim-targeting-1"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Passage:
    ref: str
    kind: str
    key: str
    text: str
    independence_group: str
    gap: int


def _entity_names(conn, entity_id: int) -> set[str]:
    row = conn.execute(
        "SELECT canonical_name FROM entities WHERE id=?", (entity_id,)).fetchone()
    names = {row["canonical_name"]} if row else set()
    for alias in conn.execute(
            "SELECT name FROM aliases WHERE entity_id=? AND status='verified'",
            (entity_id,)):
        names.add(alias["name"])
    return {name for name in names if len(name) >= 2}


def _spans(text: str, names: set[str]) -> list[tuple[int, int]]:
    return sorted(
        (match.start(), match.end())
        for name in names for match in re.finditer(re.escape(name), text))


def _closest_gap(text: str, left: set[str], right: set[str]) -> int | None:
    """两端各自出现且不是同一处文字时的最近距离。

    中文里一端常是另一端的子串（回归/线性回归、梯度下降/梯度下降法）。命中位置
    重叠说明只是同一处文字被两个名字各匹配了一次，不算共现，必须排除。
    """
    left_spans = _spans(text, left)
    right_spans = _spans(text, right)
    if not left_spans or not right_spans:
        return None
    best = None
    for a_start, a_end in left_spans:
        for b_start, b_end in right_spans:
            if a_start < b_end and b_start < a_end:
                continue
            gap = (b_start - a_end) if b_start >= a_end else (a_start - b_end)
            if best is None or gap < best:
                best = gap
    return best


def supporting_groups(conn, claim_id: int) -> set[str]:
    return {row["g"] for row in conn.execute(
        "SELECT DISTINCT src.independence_group g FROM evidence e"
        " JOIN source_snapshots ss ON ss.id=e.source_snapshot_id"
        " JOIN sources src ON src.id=ss.source_id"
        " WHERE e.claim_id=?", (claim_id,))}


def _probed(conn, claim_id: int) -> set[tuple[str, str]]:
    return {(row["ref"], row["content_hash"]) for row in conn.execute(
        "SELECT ref,content_hash FROM targeting_probes WHERE claim_id=?",
        (claim_id,))}


def record_probe(conn, claim_id: int, passage: "Passage", *, result: str,
                 run_id: int | None = None, reason: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO targeting_probes"
        " (claim_id,ref,content_hash,run_id,result,reason,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (claim_id, passage.ref, _hash(passage.text), run_id, result,
         reason[:300], time.time()))
    conn.commit()


def find_passages(conn, claim_id: int, *, window: int = WINDOW,
                  exclude_known_groups: bool = True,
                  skip_probed: bool = True, limit: int = 3) -> list[Passage]:
    """机械共现检索：两端概念在同一段且相距不超过 window 字。"""
    claim = store.get_claim(conn, claim_id)
    if not claim:
        raise ValueError(f"Claim 不存在: {claim_id}")
    left = _entity_names(conn, claim.subject_id)
    right = _entity_names(conn, claim.object_id)
    if not left or not right:
        return []
    known = supporting_groups(conn, claim_id) if exclude_known_groups else set()
    seen = _probed(conn, claim_id) if skip_probed else set()
    found = []
    for item in local_corpus.passages(conn):
        if item.independence_group in known:
            continue
        if (item.ref, _hash(item.text)) in seen:
            continue
        gap = _closest_gap(item.text, left, right)
        if gap is None or gap > window:
            continue
        found.append(Passage(
            item.ref, item.kind, item.key, item.text,
            item.independence_group, gap))
    found.sort(key=lambda item: (item.gap, item.ref))
    return found[:limit]


def stuck_claims(conn, *, limit: int | None = None) -> list[int]:
    """最新裁决为 needs_more_evidence 的 claim。"""
    rows = conn.execute(
        "SELECT d.target_id FROM decisions d"
        " JOIN (SELECT target_id,MAX(id) mid FROM decisions"
        "       WHERE target_type='claim' GROUP BY target_id) m ON m.mid=d.id"
        " WHERE d.outcome='needs_more_evidence' ORDER BY d.target_id"
        + (" LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
    return [row["target_id"] for row in rows]


def survey(conn, *, limit: int | None = None, window: int = WINDOW,
           passages: int = 3) -> dict:
    """零 LLM 勘察：哪些证据不足的 Claim 有可读的新来源段落。"""
    result = []
    for claim_id in stuck_claims(conn, limit=limit):
        claim = store.get_claim(conn, claim_id)
        subject = store.get_entity(conn, claim.subject_id)
        object_ = store.get_entity(conn, claim.object_id)
        hits = find_passages(conn, claim_id, window=window, limit=passages)
        result.append({
            "claim_id": claim_id,
            "claim": f"{subject.canonical_name} -{claim.relation}-> {object_.canonical_name}",
            "supporting_groups": sorted(supporting_groups(conn, claim_id)),
            "candidates": [
                {"ref": item.ref, "group": item.independence_group, "gap": item.gap}
                for item in hits],
        })
    return {
        "examined": len(result),
        "with_candidates": sum(1 for item in result if item["candidates"]),
        "claims": result,
    }


def _extract_one(payload: dict, passage_text: str, subject, object_) -> dict | None:
    """把模型回答校验成一条可入库的 Claim 观察；不合格返回 None。"""
    relation = str(payload.get("relation", "none")).strip()
    if relation in {"", "none"}:
        return None
    # 端点比对走 normalized_name，不做裸字符串相等。
    subject_name = store.normalize_name(str(payload.get("subject", "")))
    object_name = store.normalize_name(str(payload.get("object", "")))
    pair = {
        store.normalize_name(subject.canonical_name),
        store.normalize_name(object_.canonical_name),
    }
    if {subject_name, object_name} != pair:
        return None
    evidence = str(payload.get("evidence", "")).strip()
    if not evidence_in_text(evidence, passage_text):
        return None
    if subject_name == store.normalize_name(subject.canonical_name):
        subject_id, object_id = subject.id, object_.id
        subject_type, object_type = subject.entity_type, object_.entity_type
    else:
        subject_id, object_id = object_.id, subject.id
        subject_type, object_type = object_.entity_type, subject.entity_type
    qualifiers = payload.get("qualifiers") if isinstance(
        payload.get("qualifiers"), dict) else {}
    try:
        registry().validate_claim(
            subject_type, relation, object_type, active_only=True)
        registry().validate_qualifiers(relation, qualifiers, require_required=True)
    except ValueError:
        return None
    return {
        "subject_id": subject_id, "object_id": object_id, "relation": relation,
        "qualifiers": qualifiers, "evidence": evidence,
        "evidence_type": str(payload.get("evidence_type", "cooccurrence")).strip(),
        "reason": str(payload.get("reason", ""))[:300],
    }


def run(conn, *, limit: int | None = None, window: int = WINDOW,
        passages: int = 2, verify_llm: bool = True) -> dict:
    """定向补证一轮：机械检索 -> 并发中性抽取 -> 串行落库 -> 重算裁决。"""
    from . import decision

    # limit 指的是「实际探查的 claim 数」。没有候选段落的 claim 不占额度，
    # 否则 --limit 5 很可能全落在没段落可读的 claim 上，白跑一轮。
    targets = []
    chosen = 0
    for claim_id in stuck_claims(conn):
        if limit is not None and chosen >= limit:
            break
        hits = find_passages(conn, claim_id, window=window, limit=passages)
        if not hits:
            continue
        claim = store.get_claim(conn, claim_id)
        subject = store.get_entity(conn, claim.subject_id)
        object_ = store.get_entity(conn, claim.object_id)
        for passage in hits:
            targets.append((claim, subject, object_, passage))
        chosen += 1
    if not targets:
        return {"examined_claims": 0, "probed_passages": 0, "new_evidence": [],
                "no_relation": [], "outcomes": {}}

    run_id = store.create_run(
        conn, "claim_targeting", ALGORITHM_VERSION, model=llm.CHAT_MODEL,
        prompt_version=PROMPT_VERSION,
        config={"window": window, "relation_registry_version": registry().version})

    contract = registry().extraction_contract()

    def ask(item):
        _, subject, object_, passage = item
        prompt = TARGET_PROMPT.format(
            left=subject.canonical_name, right=object_.canonical_name,
            relations=contract, passage=passage.text)
        try:
            return llm.chat_json([{"role": "user", "content": prompt}])
        except (RuntimeError, ValueError) as exc:
            return exc

    answers = llm.pmap(ask, targets)

    added, skipped, touched = [], [], []
    for (claim, subject, object_, passage), answer in zip(targets, answers):
        label = f"{subject.canonical_name} -{claim.relation}-> {object_.canonical_name}"
        if isinstance(answer, Exception) or not isinstance(answer, dict):
            reason = f"抽取失败：{answer}"
            # 失败不记 probe：下一轮应该重试，模型返回非法 JSON 不是结论。
            skipped.append({"claim_id": claim.id, "ref": passage.ref,
                            "reason": reason})
            continue
        parsed = _extract_one(answer, passage.text, subject, object_)
        if not parsed:
            reason = str(answer.get("reason", "未陈述该关系"))[:200]
            record_probe(conn, claim.id, passage, result="no_relation",
                         run_id=run_id, reason=reason)
            skipped.append({
                "claim_id": claim.id, "ref": passage.ref, "reason": reason})
            continue
        snapshot = local_corpus.snapshot_for(
            conn, passage.kind, passage.key, passage.text)
        try:
            new_claim = store.add_claim(
                conn, parsed["subject_id"], parsed["relation"], parsed["object_id"],
                qualifiers=parsed["qualifiers"], status="proposed",
                metadata={"created_from": "claim_targeted_search"})
            store.add_evidence(
                conn, snapshot.id, parsed["evidence"], parsed["evidence_type"],
                claim_id=new_claim.id, location=passage.ref,
                mechanically_valid=True, extraction_run_id=run_id,
                metadata={"targeted_for_claim": claim.id, "gap": passage.gap})
        except ValueError as exc:
            record_probe(conn, claim.id, passage, result="no_relation",
                         run_id=run_id, reason=str(exc))
            skipped.append({"claim_id": claim.id, "ref": passage.ref,
                            "reason": str(exc)})
            continue
        record_probe(conn, claim.id, passage, result="evidence", run_id=run_id,
                     reason=parsed["reason"])
        touched.append(new_claim.id)
        added.append({
            "claim_id": new_claim.id, "same_claim": new_claim.id == claim.id,
            "for": label, "ref": passage.ref, "group": passage.independence_group,
            "relation": parsed["relation"], "evidence": parsed["evidence"][:120],
        })
    store.finish_run(conn, run_id, "completed")

    entailment = []
    if verify_llm and touched:
        entailment = validators.verify_entailment_batch(
            conn, list(dict.fromkeys(touched)))
    outcomes: dict[str, int] = {}
    shadows = []
    for claim_id in dict.fromkeys(touched + [item[0].id for item in targets]):
        result = decision.shadow_claim(conn, claim_id)
        shadows.append(result)
        outcomes[result["outcome"]] = outcomes.get(result["outcome"], 0) + 1
    return {
        "run_id": run_id,
        "examined_claims": len({item[0].id for item in targets}),
        "probed_passages": len(targets),
        "new_evidence": added,
        "no_relation": skipped,
        "entailment": entailment,
        "outcomes": outcomes,
    }
