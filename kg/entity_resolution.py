"""实体对齐：确定性精确匹配优先，LLM 只处理未命中与歧义。"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Callable

from . import alias_evidence, llm, store
from .observations import EntityObservation


# 7：确定性快路收窄到「一个名字是另一个的形式变体或加类别后缀」；同词根异中心词
#    （机器学习方法／机器学习算法）改走反向复核，高置信度 existing 落成语境链接
#    而不再阻塞整条 Claim。语义变了，旧事件不能当作本版结论看。
RESOLVER_VERSION = "entity-resolver-7"
ALIGNMENT_POLICY_VERSION = "entity-alignment-policy-4"
LLM_SUSPECT_CONFIDENCE = 0.80
LLM_AUTO_LINK_CONFIDENCE = 0.95
# existing 和 new 都只有达到统一的自动执行门槛才允许落地。低于门槛说明系统
# 暂时不能安全执行，不说明 observation 无效。
LLM_NEW_ENTITY_CONFIDENCE = LLM_AUTO_LINK_CONFIDENCE
# 反向复核说「该分开」时的采信门槛。它只能否决，所以门槛可以比正向低——
# 误否决的代价是多送一条给人看，误采信的代价是把两个知识对象焊死。
SEPARATION_OBJECTION_CONFIDENCE = 0.60
MAX_CANDIDATES = 5
# 每个候选给 LLM 看的代表性原文与代表关系条数。
CANDIDATE_EXCERPTS = 3
CANDIDATE_RELATIONS = 4
# 这些 outcome 都没有否定 observation 本身，不能把 observation 标成 rejected。
NON_TERMINAL_OUTCOMES = frozenset({
    "suspected_same_entity",
    "ambiguous",
    "below_confidence",
    "resolver_error",
    "invalid_response",
})
# 这两类已有语义判断，但不足以自动执行。同一 resolver 版本下不反复花费 LLM，
# 等人工复核、策略升级或 resolver 版本变化后再重放。
NEEDS_REVIEW_OUTCOMES = frozenset({"ambiguous", "below_confidence"})
SAFE_DIRECT_MATCH_TYPES = {"translation_alias", "name_variant"}
MATCH_TYPES = {
    "translation_alias", "name_variant", "abbreviation", "symbol",
    "composite", "semantic_alias", "none",
}
# 类别标记后缀。剥掉它们只用于两件事：召回候选，以及判断"一个名字是不是另一个
# 加了类别词"。它们本身不证明同一性——见 `_suffix_containment` 的注释。
NAME_VARIANT_SUFFIXES = (
    "问题", "方法", "算法", "模型", "函数", "运算", "任务", "估计",
    "流程", "过程", "器", "法",
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
    # 这次判定写下的 entity_resolution_events 行。物化留痕挂在它上面，
    # 判错之后 `store.revert_materialization` 靠它找回要撤销的行。
    event_id: int | None = None
    # 这次判定顺手登记的观察名别名。事件 id 要等 `_record` 才有，所以先带出来，
    # 由 `_record` 一并挂进留痕——否则撤销时这条别名会留在库里指着错实体。
    alias_id: int | None = None


LLMNormalizer = Callable[[EntityObservation, list[dict]], dict]
SeparationVerifier = Callable[[EntityObservation, dict], dict]


def _compact_name(value: str) -> str:
    """去掉不承载语义的形式差异：全半角、大小写、空白、连接标点。"""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s`'\"_\-—–·()（）]+", "", folded)


def _identity_forms(value: str) -> set[str]:
    """同一个名称的纯形式变体。

    只有一条会改变字符数的规则：修饰性「的」。中文名称里的「的」是连接成分，
    「通用人工智能的生存风险」和「通用人工智能生存风险」指同一个东西。删它不会
    像删「方法／算法」那样丢掉一个语义中心词，所以放在第一档而不是第二档。

    残留风险是「的」出现在词内的名字（目的、的确）。要踩中得同时满足：删「的」
    后的残句恰好是另一个已存在实体的完整名称，且 LLM 已经独立判定这两者同一。
    """
    compact = _compact_name(value)
    return {compact, compact.replace("的", "")} - {""}


def _strip_category_suffixes(compact: str) -> set[str]:
    roots = {compact}
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


def _name_variant_roots(value: str) -> set[str]:
    """剥掉类别后缀得到的词根集合。**只用于召回候选和识别高风险名称对。**"""
    roots: set[str] = set()
    for form in _identity_forms(value):
        roots |= _strip_category_suffixes(form)
    return roots


def _suffix_containment(alias: str, canonical: str) -> bool:
    """一个名字是不是另一个加了类别后缀。

    判据是**包含**，不是词根集合相交。相交只要求两个名字各剥各的、剥到中间某处
    碰头：「机器学习方法」与「机器学习算法」都能剥到「机器学习」，于是被判成同
    一个东西。但谁也不是谁的前缀，共同词根只说明它们谈的是同一个领域。

    包含则要求一方的**完整名称**出现在另一方的词根集合里：「反向传播算法」剥掉
    「算法」正好是「反向传播」。这才是"后缀冗余"的确切形状。
    """
    left, right = _identity_forms(alias), _identity_forms(canonical)
    if left & right:
        return True
    return bool(left & _name_variant_roots(canonical)
                or right & _name_variant_roots(alias))


def _is_sibling_head_variant(alias: str, canonical: str) -> bool:
    """同词根、异类别中心词——「机器学习方法」对「机器学习算法」。

    这是确定性规则永远判不了的一档：两个名字都在谈同一个词根，但各自挂了不同的
    类别中心词，究竟是同义换词还是两个该分别建点的对象，只有语境说了算。标出来
    是为了送反向复核，不是为了否定。
    """
    if _suffix_containment(alias, canonical):
        return False
    return bool(_name_variant_roots(alias) & _name_variant_roots(canonical))


def _needs_separation_review(alias: str, canonical: str) -> bool:
    """这对名称能不能在一轮提问后就当场落地。

    两类不能：

    - 同词根异中心词（机器学习方法／机器学习算法）；
    - 一个名字是另一个加了**非类别**的修饰成分（softmax-交叉熵损失／交叉熵损失，
      欧盟人工智能法案／人工智能法案）。剥掉类别后缀是安全的，剥掉限定词不是
      ——限定词往往正是区别本身。

    两类共有的形状是：字符上高度相关，语义上可能相差一个知识对象。正向提问在这
    里最容易点头，所以反向再问一次。
    """
    if _suffix_containment(alias, canonical):
        return False
    if _is_sibling_head_variant(alias, canonical):
        return True
    left, right = _compact_name(alias), _compact_name(canonical)
    return left != right and (left in right or right in left)


def _has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _validated_direct_match_type(alias: str, canonical: str,
                                 claimed_type: str, *,
                                 observed_type: str = "",
                                 entity_type: str = "") -> str | None:
    """LLM 只能提议快捷类型；最终资格由确定性字符串规则确认。

    这条规则**不发起合并**，只否决 LLM 的提议：调用方必须已经拿到一条置信度够高
    的 `existing`，这里才有机会把它升成 `safe_direct`。所以它挡住的是模型编出来
    的理由——库里有一条 reason 里写的名字根本不存在，规则照样只按名字判对了。

    这是**充分条件**：命中即可判定同名。命中不了不代表不同名——那种情况走语境
    链接或 alias_evidence 的语料声明累计，不要当成否定结论。

    `NAME_VARIANT_SUFFIXES` 剥掉的恰好是类型标记（问题／任务→task，
    方法／算法／模型→solution，函数→多半 concept），所以后缀剥离会抹掉主类型
    要表达的区别：「回归问题」是 task 而「回归」是 solution，「概率模型」是
    solution 而「概率」是 concept。判据因此是**剥掉后缀会不会改变主类型**——
    不变说明后缀冗余，该合；变了说明后缀带类型，不能走自动执行的快路。

    只在两边类型都已知时才拦。观察类型是 LLM 给的、可能错，所以这里不做否定
    结论，只是取消快路资格：调用方会退回累计对齐，别名留在 proposed 等复核。
    """
    if _suffix_containment(alias, canonical):
        if observed_type and entity_type and observed_type != entity_type:
            return None
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
    return _attach_candidate_context(conn, ranked[:limit])


def _attach_candidate_context(conn, candidates: list[dict]) -> list[dict]:
    """给候选补上真实用法：几条原文摘录和几条已有关系。

    只有规范名、类型和一句定义时，模型判的是"这两个名字听起来像不像同一个东西"，
    那正是字符串相似度已经做过的事。判语境同一性得看这个实体在语料里实际怎么被
    使用——候选侧不给上下文，语境判断就只剩一侧有语境。

    排完序才查，所以代价是 K 条而不是全库。
    """
    if not candidates:
        return candidates
    ids = [item["id"] for item in candidates]
    placeholders = ",".join("?" * len(ids))
    for item in candidates:
        item["excerpts"] = []
        item["relations"] = []
    by_id = {item["id"]: item for item in candidates}
    for row in conn.execute(
            "SELECT entity_id,excerpt FROM evidence"
            f" WHERE entity_id IN ({placeholders}) ORDER BY id", ids):
        bucket = by_id[row["entity_id"]]["excerpts"]
        if len(bucket) < CANDIDATE_EXCERPTS:
            bucket.append(row["excerpt"][:200])
    for row in conn.execute(
            "SELECT c.subject_id,c.relation,c.object_id,"
            " s.canonical_name sname,o.canonical_name oname FROM claims c"
            " JOIN entities s ON s.id=c.subject_id"
            " JOIN entities o ON o.id=c.object_id"
            f" WHERE c.status!='rejected' AND (c.subject_id IN ({placeholders})"
            f" OR c.object_id IN ({placeholders})) ORDER BY c.id", (*ids, *ids)):
        edge = f"{row['sname']} -{row['relation']}-> {row['oname']}"
        for endpoint in (row["subject_id"], row["object_id"]):
            item = by_id.get(endpoint)
            if item is not None and len(item["relations"]) < CANDIDATE_RELATIONS:
                item["relations"].append(edge)
    return candidates


# 中文短词的 SequenceMatcher 比值偏低（感知机/感知器只有 0.667），阈值按中文调。
DUPLICATE_SCAN_THRESHOLD = 0.6


def find_duplicate_candidates(conn, *, threshold: float = DUPLICATE_SCAN_THRESHOLD,
                              limit: int = 50) -> list[dict]:
    """机械扫描已有实体里的疑似重复对（零 LLM，只报告不改数据）。

    entity_alignment_candidates 只在 resolve 时由 LLM 提议才产生，不会回头看
    已有实体；即使采用高门槛，新建仍可能重复。这个扫描补上事后发现通道。
    """
    rows = conn.execute(
        "SELECT id,canonical_name,normalized_name,entity_type,definition,metadata"
        " FROM entities WHERE status!='rejected' ORDER BY id").fetchall()
    aliases: dict[int, set[str]] = {}
    for row in conn.execute(
            "SELECT entity_id,normalized_name FROM aliases WHERE status!='rejected'"):
        aliases.setdefault(row["entity_id"], set()).add(row["normalized_name"])
    known = {
        (item["observed_name"], item["entity_id"]) for item in conn.execute(
            "SELECT observed_name,entity_id FROM entity_alignment_candidates")
    }
    pairs = []
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            names_left = {left["normalized_name"]} | aliases.get(left["id"], set())
            names_right = {right["normalized_name"]} | aliases.get(right["id"], set())
            shared = names_left & names_right
            score = max(
                (SequenceMatcher(None, a, b).ratio()
                 for a in names_left for b in names_right), default=0.0)
            # 一个名字完全包含另一个（回归/线性回归）也是常见的重复来源。
            contained = any(
                a != b and (a in b or b in a)
                for a in names_left for b in names_right)
            if not shared and not contained and score < threshold:
                continue
            metadata_left = json.loads(left["metadata"] or "{}")
            metadata_right = json.loads(right["metadata"] or "{}")
            pairs.append({
                "left_id": left["id"], "left": left["canonical_name"],
                "left_type": left["entity_type"],
                "right_id": right["id"], "right": right["canonical_name"],
                "right_type": right["entity_type"],
                "score": round(score, 3),
                "shared_names": sorted(shared),
                "name_contained": contained,
                "same_type": left["entity_type"] == right["entity_type"],
                # 低置信度新建的实体重复风险更高，排前面。
                "low_confidence_creation": bool(
                    metadata_left.get("below_auto_link_confidence")
                    or metadata_right.get("below_auto_link_confidence")),
                "already_queued": bool(
                    (right["canonical_name"], left["id"]) in known
                    or (left["canonical_name"], right["id"]) in known),
            })
    pairs.sort(key=lambda item: (
        not item["low_confidence_creation"], not item["shared_names"],
        -item["score"]))
    return pairs[:limit]


# 名称在多次语境里落到哪里，就是全局别名该问的问题。
STABILITY_UNRESOLVED = frozenset({
    "ambiguous", "below_confidence", "suspected_same_entity",
    "resolver_error", "invalid_response", "reverted",
})


def mention_stability_report(conn, *, limit: int = 200, min_contexts: int = 2,
                             current_resolver_only: bool = False) -> dict:
    """同一个表面名称在多次真实语境中是否稳定指向同一个实体——只读，零 LLM。

    全局别名该问的是这个。累计打分（`store.add_alignment_evidence`）早就是按
    `independence_group` 折算的，同一来源重复一百次也只算一次；这里补的是它算不
    出来的那部分：这个名字有没有**竞争目标**，有多少次根本没判下来。一个名字在
    两个实体之间摇摆，单看任一侧的累计分都可能很高。

    报告混代：事件带着产生它们时的 resolver 版本，`resolver_versions` 把这件事
    摆在明面上，别拿旧版本的分布替当前策略背书。
    """
    sql = (
        "SELECT ev.deterministic_name,ev.raw_name,ev.outcome,ev.entity_id,"
        " ev.selected_candidate_id,ev.resolver_version,s.independence_group"
        " FROM entity_resolution_events ev"
        " LEFT JOIN source_snapshots ss ON ss.id=ev.source_snapshot_id"
        " LEFT JOIN sources s ON s.id=ss.source_id")
    params: tuple = ()
    if current_resolver_only:
        sql += " WHERE ev.resolver_version=?"
        params = (RESOLVER_VERSION,)
    grouped: dict[str, dict] = {}
    for row in conn.execute(sql + " ORDER BY ev.id", params):
        item = grouped.setdefault(row["deterministic_name"], {
            "name": row["deterministic_name"], "surface_names": set(),
            "targets": {}, "unresolved": 0, "groups": set(),
            "resolver_versions": set(),
        })
        item["surface_names"].add(row["raw_name"])
        item["resolver_versions"].add(row["resolver_version"])
        if row["independence_group"]:
            item["groups"].add(row["independence_group"])
        target = row["entity_id"] or row["selected_candidate_id"]
        if row["outcome"] in STABILITY_UNRESOLVED:
            item["unresolved"] += 1
        if target is None:
            continue
        item["targets"][target] = item["targets"].get(target, 0) + 1

    names = {row["id"]: row["canonical_name"] for row in conn.execute(
        "SELECT id,canonical_name FROM entities")}
    verified = {
        row["normalized_name"] for row in conn.execute(
            "SELECT normalized_name FROM aliases WHERE status='verified'")}
    canonical = {
        row["normalized_name"] for row in conn.execute(
            "SELECT normalized_name FROM entities")}
    items = []
    for item in grouped.values():
        contexts = sum(item["targets"].values())
        if contexts < min_contexts or item["name"] in canonical:
            continue
        ranked = sorted(item["targets"].items(), key=lambda kv: (-kv[1], kv[0]))
        dominant_id, dominant_count = ranked[0]
        items.append({
            "name": item["name"],
            "surface_names": sorted(item["surface_names"]),
            "context_count": contexts,
            "dominant_target": names.get(dominant_id, str(dominant_id)),
            "dominant_target_id": dominant_id,
            "dominant_target_count": dominant_count,
            "dominant_target_ratio": round(dominant_count / contexts, 3),
            "competing_target_count": len(ranked) - 1,
            "competing_targets": [
                {"entity": names.get(entity_id, str(entity_id)), "count": count}
                for entity_id, count in ranked[1:]],
            "unresolved_count": item["unresolved"],
            "independent_source_groups": sorted(item["groups"]),
            "resolver_versions": sorted(item["resolver_versions"]),
            "alias_status": (
                "verified" if item["name"] in verified else "proposed"),
        })
    items.sort(key=lambda row: (
        -row["competing_target_count"], -row["context_count"], row["name"]))
    return {
        "names": len(items),
        "with_competing_targets": sum(
            1 for row in items if row["competing_target_count"]),
        "stable_but_unverified": sum(
            1 for row in items
            if not row["competing_target_count"]
            and len(row["independent_source_groups"]) >= 2
            and row["alias_status"] != "verified"),
        "next": "稳定且跨够独立来源组的可考虑转正；有竞争目标的先看 duplicates",
        "items": items[:limit],
    }


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
   且上下文含义没有变化时，match_type=name_variant。但如果增删该词改变了实体的
   主类型，含义就变了，不是 name_variant——“回归问题”是 task 而“回归”是
   solution，“概率模型”是 solution 而“概率”是 concept，这类必须判 different
   或 ambiguous。
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


SEPARATION_VERDICTS = {"should_stay_separate", "no_stable_difference", "uncertain"}


def _verify_separation_with_llm(observation: EntityObservation,
                                selected: dict) -> dict:
    """反向复核：请模型优先找出**不能合并**的理由。

    这不是拿第二次调用充第二个来源——同一个模型问两遍不产生独立性，两轮结论都
    只记进消歧事件，不进 `entity_alignment_evidence` 的独立来源计数。它提高的是
    同一次语境判断的可靠性：正向提问天然偏向找相同点，换个方向问一次，能把"听着
    像同义词"和"真是同一个知识对象"分开。

    方向上的不对称和 `is_strong_evidence`、`knowledge_objection` 同构：这一轮
    只能否决，不能支持。它说"没有稳定区别"不会让一条判定升级，只是不再拦它。
    """
    prompt = f"""你在复核两个名称是否值得在教学知识图谱里分别建点。

名称 A：{observation.name}
A 的定义（依据当前语料）：{observation.definition}
A 的原文证据：{observation.evidence}

名称 B（图谱中已有实体）：{selected.get('canonical_name', '')}
B 的类型：{selected.get('entity_type', '')}
B 的定义：{selected.get('definition', '')}
B 的原文用法：{json.dumps(selected.get('excerpts', []), ensure_ascii=False)}
B 的已有关系：{json.dumps(selected.get('relations', []), ensure_ascii=False)}

这两个名称共享词根但类别中心词不同（例如「方法」对「算法」、「模型」对
「分类器」、「问题」对「任务」）。这类名称经常是同义换词，也经常是两个确实
不同的知识对象。

**请优先寻找不能合并的理由。** 依次检查：
1. 两者的外延是否不同（一个是做法、另一个是这类做法的产物或载体）？
2. 教学上是否需要分别讲解、分别考察、分别列先修？
3. 语料中是否在同一处同时使用了这两个说法来指不同的东西？
4. 合并之后，B 已有的关系是否会有任何一条变得不成立？

只有当你找不到任何稳定、可复用、有教学价值的区别时，才输出
no_stable_difference。证据不足以判断就输出 uncertain，不要猜。

只输出 JSON：
{{
  "verdict": "should_stay_separate|no_stable_difference|uncertain",
  "confidence": 0.0,
  "reason": "简短理由；should_stay_separate 时必须说出区别是什么"
}}"""
    payload = llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(payload, dict):
        raise ValueError("反向复核器必须返回 JSON object")
    return payload


def _separation_objection(verifier: SeparationVerifier | None,
                          observation: EntityObservation,
                          selected: dict) -> dict:
    """跑一次反向复核，把结果规整成 verdict/confidence/reason。"""
    run = verifier or _verify_separation_with_llm
    try:
        payload = run(observation, selected)
    except Exception as exc:
        # 调用失败不是结论。当作 uncertain：不否决，但也不放行到语境链接。
        return {"verdict": "uncertain", "confidence": 0.0,
                "reason": f"反向复核调用失败：{exc}"}
    verdict = str(payload.get("verdict", "uncertain")).strip()
    if verdict not in SEPARATION_VERDICTS:
        verdict = "uncertain"
    try:
        confidence = min(1.0, max(0.0, float(payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "verdict": verdict, "confidence": confidence,
        "reason": str(payload.get("reason", "")).strip() or "反向复核未给出理由",
    }


def _record(conn, observation: EntityObservation, result: Resolution, *,
            source_snapshot_id: int | None,
            observation_id: int | None) -> Resolution:
    event_id = store.add_resolution_event(
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
    store.add_materialization(
        conn, observation_id, "alias", result.alias_id,
        resolution_event_id=event_id, outcome=result.outcome)
    return replace(result, event_id=event_id)


def _matched_result(entity, observation: EntityObservation, *, matched_by: str,
                    reason: str, candidate_ids: tuple[int, ...] = (),
                    confidence: float | None = None,
                    same_outcome: str = "same_entity") -> Resolution:
    conflict = entity.entity_type != observation.entity_type
    return Resolution(
        entity.id, "type_conflict" if conflict else same_outcome,
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
            llm_normalizer: LLMNormalizer | None = None,
            llm_separation_verifier: SeparationVerifier | None = None) -> Resolution:
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
        # 调用失败不是结论——模型没说这个名字有歧义，它根本没回答。传输层已经退避
        # 重试过（llm._post），走到这里说明重试也没成，留给下一轮重放，不是 ambiguous。
        result = Resolution(
            None, "resolver_error", f"LLM 规范化失败：{exc}",
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

    if decision == "ambiguous":
        result = Resolution(
            None, "ambiguous", reason,
            matched_by="llm_ambiguous",
            normalized_name=canonical_name or deterministic_name,
            candidate_ids=candidate_ids, confidence=confidence)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    if decision not in {"existing", "new"}:
        result = Resolution(
            None, "invalid_response",
            f"{reason}；decision 必须是 existing、new 或 ambiguous",
            matched_by="llm_invalid_response",
            normalized_name=canonical_name or deterministic_name,
            candidate_ids=candidate_ids, confidence=confidence)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    if decision == "existing" and not selected:
        result = Resolution(
            None, "invalid_response",
            f"{reason}；existing 必须指向候选实体",
            matched_by="llm_invalid_response",
            normalized_name=canonical_name or deterministic_name,
            candidate_ids=candidate_ids, confidence=confidence)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    if selected and confidence >= LLM_SUSPECT_CONFIDENCE:
        if selected.id not in candidate_ids:
            candidate_ids = (*candidate_ids, selected.id)
        direct_match_type = _validated_direct_match_type(
            observation.name, selected.canonical_name, match_type,
            observed_type=observation.entity_type,
            entity_type=selected.entity_type)
        safe_direct = (
            direct_match_type in SAFE_DIRECT_MATCH_TYPES
            and confidence >= LLM_AUTO_LINK_CONFIDENCE)
        if safe_direct:
            match_type = direct_match_type

        # 高风险名称对（同词根异中心词、非类别修饰成分）必须再反向问一次，
        # 两轮一致才允许当场落地。
        contextual_allowed = confidence >= LLM_AUTO_LINK_CONFIDENCE
        if (not safe_direct and contextual_allowed
                and _needs_separation_review(
                    observation.name, selected.canonical_name)):
            objection = _separation_objection(
                llm_separation_verifier, observation,
                candidate_by_id.get(selected.id, {
                    "canonical_name": selected.canonical_name,
                    "entity_type": selected.entity_type,
                    "definition": selected.definition,
                }))
            reason = (f"{reason}；反向复核 [{objection['verdict']}"
                      f"/{objection['confidence']:.2f}] {objection['reason']}")
            if (objection["verdict"] == "should_stay_separate"
                    and objection["confidence"] >= SEPARATION_OBJECTION_CONFIDENCE):
                # 反向复核找到了稳定区别。不记对齐证据——被否决的一轮不是
                # 「这两个名字疑似同一」的佐证。
                result = Resolution(
                    None, "ambiguous", reason,
                    matched_by="separation_objection",
                    normalized_name=canonical_name or deterministic_name,
                    candidate_ids=candidate_ids, confidence=confidence,
                    selected_candidate_id=selected.id)
                return _record(
                    conn, observation, result,
                    source_snapshot_id=source_snapshot_id,
                    observation_id=observation_id)
            contextual_allowed = objection["verdict"] == "no_stable_difference"

        alias_id = None
        if deterministic_name != selected.normalized_name:
            alias_id = store.add_alias(
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
            result = replace(_matched_result(
                selected, observation, matched_by=matched_by,
                reason=reason, candidate_ids=candidate_ids,
                confidence=confidence), alias_id=alias_id)
            return _record(
                conn, observation, result, source_snapshot_id=source_snapshot_id,
                observation_id=observation_id)
        if contextual_allowed:
            # 本次 mention 指向这个实体，**不**等于这个字符串永远指向它。
            # 两个门槛因此分开：语境链接只要这一次的证据和定义对得上，全局别名
            # 要这个名字在多个独立来源的语境里稳定地指向同一个实体。别名留在
            # proposed 继续累计，Claim 不再陪着一起卡住。
            result = _matched_result(
                selected, observation, matched_by="llm_contextual",
                reason=(
                    f"{reason}；本次语境判定指向既有实体，别名累计分"
                    f" {alignment['score']:.3f}，独立来源"
                    f" {alignment['independent_sources']}/2，未达全局别名门槛"),
                candidate_ids=candidate_ids, confidence=confidence,
                same_outcome="contextual_same_entity")
            result = replace(result, alias_id=alias_id)
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
            selected_candidate_id=selected.id, alias_id=alias_id)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    if decision == "new" and not canonical_name:
        result = Resolution(
            None, "invalid_response",
            f"{reason}；new 必须提供 canonical_name",
            matched_by="llm_invalid_response",
            normalized_name=deterministic_name,
            candidate_ids=candidate_ids, confidence=confidence)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    if decision == "new" and confidence >= LLM_NEW_ENTITY_CONFIDENCE:
        entity = store.add_entity(
            conn, canonical_name, observation.entity_type,
            definition=observation.definition, status="proposed",
            metadata={
                "created_from": "llm_normalized_grounded_observation",
                "creation_confidence": confidence,
                # 低于合并门槛建出来的实体重复风险更高，标出来供重复清扫优先看。
                "below_auto_link_confidence":
                    confidence < LLM_AUTO_LINK_CONFIDENCE,
            })
        alias_id = None
        if store.normalize_name(observation.name) != entity.normalized_name:
            alias_id = store.add_alias(
                conn, entity.id, observation.name,
                source_snapshot_id=source_snapshot_id, status="proposed",
                evidence_excerpt=observation.evidence)
        result = Resolution(
            entity.id, "created", reason, matched_by="llm_new",
            normalized_name=entity.normalized_name, candidate_ids=candidate_ids,
            confidence=confidence, alias_id=alias_id)
        return _record(
            conn, observation, result, source_snapshot_id=source_snapshot_id,
            observation_id=observation_id)

    result = Resolution(
        None, "below_confidence",
        f"{reason}；置信度 {confidence:.2f} 未达到自动执行阈值"
        f" {LLM_AUTO_LINK_CONFIDENCE:.2f}",
        matched_by="llm_below_confidence",
        normalized_name=canonical_name or deterministic_name,
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
        corpus_evidence = None
        if safe:
            store.add_alignment_evidence(
                conn, observed_name=row["name"], entity_id=row["entity_id"],
                confidence=confidence,
                policy_version=ALIGNMENT_POLICY_VERSION,
                resolver_version=RESOLVER_VERSION,
                reason=f"[{match_type}] {reason}",
                source_snapshot_id=row["source_snapshot_id"],
                direct_verify=True)
        elif status == "proposed":
            # 走不了确定性快路不等于不是别名。看语料有没有显式声明，
            # 跨够独立来源组由累计逻辑转正。
            corpus_evidence = alias_evidence.record(
                conn, row["alias_id"],
                policy_version=ALIGNMENT_POLICY_VERSION,
                resolver_version=RESOLVER_VERSION)
            status = corpus_evidence["status"]
        results.append({
            "alias_id": row["alias_id"],
            "alias": row["name"],
            "entity": row["canonical_name"],
            "decision": decision,
            "match_type": match_type,
            "confidence": confidence,
            "status": status,
            "reason": reason,
            "corpus_declarations": corpus_evidence,
        })
    return results


def _review_alignment_with_llm(row: dict) -> dict:
    prompt = f"""你在复核一个知识图谱的疑似同实体候选。

观察名称：{row['observed_name']}
目标规范名：{row['canonical_name']}
目标类型：{row['entity_type']}
目标定义：{row['definition']}
已有来源证据：
{json.dumps(row['evidence'], ensure_ascii=False)}

严格规则：
1. 只有指向完全同一概念才是 same；相关、上下位、组成关系都是 different。
2. 中英文直接互译可标 translation_alias。
3. 仅增加或省略“问题、方法、算法、模型、函数、运算、任务”等类别词，
   且含义不变，可标 name_variant。若增删该词改变了实体主类型（“回归问题”是
   task 而“回归”是 solution），含义就变了，不能标 name_variant。
4. 缩写、符号、语义别名、组合概念分别如实标注，不得伪装成直接译名或名称变体。
5. 证据不足必须 uncertain。

只输出 JSON：
{{
  "verdict": "same|different|uncertain",
  "match_type": "translation_alias|name_variant|abbreviation|symbol|composite|semantic_alias|none",
  "confidence": 0.0,
  "reason": "简短理由"
}}"""
    payload = llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(payload, dict):
        raise ValueError("实体对齐复核器必须返回 JSON object")
    return payload


def review_suspected_alignments(conn, limit: int = 50) -> list[dict]:
    """复核 suspected 对齐；M3 结论留痕，仅确定性可验证的直接别名自动升级。"""
    rows = conn.execute(
        "SELECT ac.id,ac.observed_name,ac.entity_id,ac.score,"
        " e.canonical_name,e.entity_type,e.definition"
        " FROM entity_alignment_candidates ac"
        " JOIN entities e ON e.id=ac.entity_id"
        " WHERE ac.status='suspected' ORDER BY ac.score DESC,ac.id"
        " LIMIT ?", (max(1, limit),)).fetchall()
    items = []
    for source_row in rows:
        row = dict(source_row)
        row["evidence"] = [
            dict(evidence) for evidence in conn.execute(
                "SELECT ae.source_snapshot_id,ae.confidence,ae.reason,"
                " s.slug source_slug,s.independence_group"
                " FROM entity_alignment_evidence ae"
                " LEFT JOIN source_snapshots ss ON ss.id=ae.source_snapshot_id"
                " LEFT JOIN sources s ON s.id=ss.source_id"
                " WHERE ae.candidate_id=? ORDER BY ae.id", (row["id"],))]
        items.append(row)

    def classify(row):
        try:
            return _review_alignment_with_llm(row)
        except Exception as exc:
            return {
                "verdict": "error", "match_type": "none", "confidence": 0.0,
                "reason": f"LLM 疑似对齐复核失败：{exc}",
            }

    outputs = llm.pmap(classify, items)
    results = []
    for row, output in zip(items, outputs):
        verdict = str(output.get("verdict", "uncertain")).strip()
        if verdict not in {"same", "different", "uncertain", "error"}:
            verdict = "uncertain"
        match_type = str(output.get("match_type", "none")).strip()
        if match_type not in MATCH_TYPES:
            match_type = "none"
        try:
            confidence = min(1.0, max(0.0, float(output.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(output.get("reason", "")).strip()
        direct_match_type = _validated_direct_match_type(
            row["observed_name"], row["canonical_name"], match_type)
        safe = (
            verdict == "same"
            and direct_match_type in SAFE_DIRECT_MATCH_TYPES
            and confidence >= LLM_AUTO_LINK_CONFIDENCE)
        if safe:
            match_type = direct_match_type
            source_snapshot_id = next(
                (item["source_snapshot_id"] for item in row["evidence"]
                 if item["source_snapshot_id"] is not None), None)
            store.add_alignment_evidence(
                conn, observed_name=row["observed_name"],
                entity_id=row["entity_id"], confidence=confidence,
                policy_version=ALIGNMENT_POLICY_VERSION,
                resolver_version=RESOLVER_VERSION,
                reason=f"[{match_type}] M3 queue review: {reason}",
                source_snapshot_id=source_snapshot_id, direct_verify=True)
            alias = conn.execute(
                "SELECT id FROM aliases WHERE entity_id=? AND normalized_name=?"
                " AND status!='rejected'",
                (row["entity_id"], store.normalize_name(row["observed_name"]))).fetchone()
            if alias:
                store.update_alias_classification(
                    conn, alias["id"], status="verified", alias_type=match_type)
        store.add_model_queue_review(
            conn, queue_type="entity_alignment", item_id=row["id"],
            model=llm.CHAT_MODEL, verdict=verdict, confidence=confidence,
            reason=reason, payload={"match_type": match_type, "safe": safe},
            policy_version=ALIGNMENT_POLICY_VERSION)
        current = conn.execute(
            "SELECT status FROM entity_alignment_candidates WHERE id=?",
            (row["id"],)).fetchone()["status"]
        results.append({
            "candidate_id": row["id"],
            "observed_name": row["observed_name"],
            "entity": row["canonical_name"],
            "verdict": verdict,
            "match_type": match_type,
            "confidence": confidence,
            "status": current,
            "auto_verified": safe,
            "reason": reason,
        })
    return results
