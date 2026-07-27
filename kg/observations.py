"""从语料快照产生并校验结构化 Observation。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import llm, store
from .ontology import registry


EXTRACT_PROMPT = """你是有据抽取器。只允许依据给出的语料，不得使用模型记忆补充知识。

覆盖主题：{topic}
允许的关系及限定字段契约：{relations}

实体主类型判据（单值必填，按编号顺序判，命中即停）：
{entity_types}

要求：
0. 每个实体先按正文写 definition，再**仅依据这条 definition** 按上面的判据判
   entity_type。不要看名字的字面就定类型：名字里有「函数」不代表它是 concept，
   有「算法」不代表它是 solution。definition 要能让人据以判型——只说「本章要介绍
   的核心主题」这种写法判不出任何东西，宁可不收这个实体。
0b. definition 要回答**它是什么**，不是它出现在哪、拿它干什么。「用于在每步决定
   如何调整参数」「线性代数章节涉及的对象」「深度学习所围绕的核心主题」说的都是
   位置和用途，不是定义——换个东西填进去同样成立的句子就不是定义。正文只说了用途
   没说是什么时，宁可不收这个实体，也不要拿用途凑一条。
1. 每个实体和 Claim 都必须附语料中的逐字摘录 evidence。
2. 每个 Claim 至少一个端点必须出现在本块 entities 中；另一个端点可以引用此前已经
   建立的实体，但名称必须可由规范名或 verified alias 唯一解析。
2b. **端点照语料的完整说法写，不得截短成上位词。**正文写「一种人工智能程序」，
   端点就是「人工智能程序」，不是「人工智能」；写「机器学习的方法分为两类」，
   说的是「机器学习的方法」，不是「机器学习」。丢掉限定词会把一条关于某类做法的
   陈述变成关于整个领域的陈述，那是另一句话。截短后的上位词若已是实体也不例外。
   如果完整说法在语料里够不上一个独立实体，就不要建这条 claim。
3. 不确定时不输出；不得把超链接、共现或章节顺序直接当成类型化关系。
4. evidence_type 只能描述证据实际表达的类型，不得夸大。
5. prerequisite_of 必须按契约填写 kind 和 strength；scope 仅在语料明确限定课程、章节或学习阶段时填写。
6. part_of 只表示真实结构部件或正文明确列出的流程阶段。“用于、依赖、参与、帮助构建、产生、输入/输出、属性、子类型”都不是 part_of；三种关系均不成立时不要建边。
7. 本文本块最多 {max_entities} 个实体、{max_claims} 个 Claim。
8. 只抽在语料里有确定所指的实体。「学习」「属性」「方法」「形状」「过程」这类通用词离开所在短语就指不到确定的东西，不要单独抽；比如语料写的是「激活函数的形状」，没有一个叫「形状」的知识点，就不要建「形状」这个实体。

输出 JSON：
{{
  "entities": [
    {{
      "name": "规范名称，照语料用词，不要把「回归问题」截短成「回归」",
      "definition": "仅按正文概括，写在 entity_type 之前，判型只依据它",
      "entity_type": "按判据编号顺序判出的那一类",
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
      "evidence_type": "{evidence_types}",
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


MIN_DEFINITION_LENGTH = 6
# 只指路、不说内容的定义。判类型的输入是定义，这类写法判不出任何一格，收进来
# 只会让主类型变成瞎猜——而主类型一旦写入就只能靠 retype 改。
#
# 判据是**中心词是否空洞**，不是句式。「一类方法」「一种损失函数」都说明了是
# 什么，放行；「核心主题」「实用技能之一」只说明了位置，拦下。
# 指向章节的定位从句。它们本身不说明内容，但常常包着真内容——「本章要完整介绍的
# 神经网络训练过程，包含定义架构、数据处理……」是一条好定义。所以先剥掉从句，
# 再看剩下的够不够，而不是见到定位语就整条丢弃。
POINTER_CLAUSES = (
    r"[，,]\s*(是|属于)?本[书章节课][^，,。.]*",
    r"^本[书章节课][^，,。.]*?的",
    r"^(后续|下一?[章节])[^，,。.]*?的",
)
# 剥完之后中心词仍然空洞的：只说了位置，没说是什么。
EMPTY_DEFINITION_PATTERNS = (
    r"(核心|主要|重要)?(主题|内容|技能|要点|议题|话题)(之一)?[。.]?$",
)


def definition_is_informative(name: str, definition: str) -> bool:
    """这条定义够不够判类型。

    不判对错，只挡住三种明显判不了的写法：太短、只是重复名字本身、只说
    「本章要介绍的……」这类指路语。
    """
    compact = re.sub(r"\s+", "", definition.strip())
    for clause in POINTER_CLAUSES:
        compact = re.sub(clause, "", compact)
    if len(compact.strip("。.，,")) < MIN_DEFINITION_LENGTH:
        return False
    if compact.strip("。.") == re.sub(r"\s+", "", name.strip()):
        return False
    return not any(re.search(pattern, compact)
                   for pattern in EMPTY_DEFINITION_PATTERNS)


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
            reg.validate_entity_type(entity_type)  # noqa: F841
        except ValueError as exc:
            rejected.append(f"entity[{index}] {exc}")
            continue
        if not name or not evidence_in_text(evidence, source_text):
            rejected.append(f"entity[{index}] 名称为空或 evidence 无法在语料中定位")
            continue
        definition = str(raw.get("definition", "")).strip()
        if not definition_is_informative(name, definition):
            rejected.append(
                f"entity[{index}]「{name}」定义不足以判定主类型：{definition!r}")
            continue
        key = store.reference_key(name)
        if key in entity_names:
            rejected.append(f"entity[{index}] 重复实体「{name}」")
            continue
        entity_names.add(key)
        entities.append(EntityObservation(
            name=name, entity_type=entity_type,
            definition=definition,
            aliases=tuple(str(a).strip() for a in raw.get("aliases", []) if str(a).strip()),
            evidence=evidence, location=str(raw.get("location", "")).strip(), raw=raw))

    entity_by_name = {store.reference_key(item.name): item for item in entities}
    for index, raw in enumerate(payload.get("claims", [])):
        subject = str(raw.get("subject", "")).strip()
        object_ = str(raw.get("object", "")).strip()
        relation = str(raw.get("relation", "")).strip()
        evidence = str(raw.get("evidence", "")).strip()
        subject_key = store.reference_key(subject)
        object_key = store.reference_key(object_)
        left = entity_by_name.get(subject_key)
        right = entity_by_name.get(object_key)
        if not left and not right:
            rejected.append(
                f"claim[{index}] 至少一个端点必须出现在本块有效 entities 中")
            continue
        # 跨块端点不查库补类型：端点类型已不是硬闸，补出来也无人消费。
        subject_type = left.entity_type if left else None
        object_type = right.entity_type if right else None
        qualifiers = (
            raw.get("qualifiers") if isinstance(raw.get("qualifiers"), dict) else {})
        try:
            reg.validate_claim_endpoint_types(
                subject_type, relation, object_type, active_only=True)
            reg.validate_qualifiers(
                relation, qualifiers, require_required=True)
        except ValueError as exc:
            rejected.append(f"claim[{index}] {exc}")
            continue
        if not evidence_in_text(evidence, source_text):
            rejected.append(f"claim[{index}] evidence 无法在语料中定位")
            continue
        evidence_type = str(raw.get("evidence_type", "cooccurrence")).strip()
        try:
            reg.validate_evidence_type(evidence_type)
        except ValueError as exc:
            rejected.append(f"claim[{index}] {exc}")
            continue
        claims.append(ClaimObservation(
            subject=subject, relation=relation, object=object_,
            qualifiers=qualifiers,
            evidence_type=evidence_type,
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
    """分块限额抽取，再跨块去重合并。

    ``max_entities`` 和 ``max_claims`` 是单个文本块的上限，而不是整章
    上限；长章节不应因为被拆分而丢失后半段已验证的候选。
    """
    chunks = split_text(source_text)
    reg = registry()

    def extract_one(chunk: str) -> ObservationBatch:
        prompt = EXTRACT_PROMPT.format(
            topic=topic,
            entity_types=reg.entity_type_contract(),
            evidence_types="|".join(reg.evidence_type_names()),
            relations=reg.extraction_contract(),
            max_entities=max_entities, max_claims=max_claims, text=chunk)
        payload = llm.chat_json([{"role": "user", "content": prompt}])
        if not isinstance(payload, dict):
            raise ValueError("抽取器必须返回 JSON object")
        return parse_payload(payload, chunk)

    batches = llm.pmap(extract_one, chunks)
    entities: dict[str, EntityObservation] = {}
    claims: dict[str, ClaimObservation] = {}
    targets: dict[str, ReadingTarget] = {}
    rejected: list[str] = []
    for batch in batches:
        for item in batch.entities:
            entities.setdefault(store.reference_key(item.name), item)
        for item in batch.claims:
            key = json.dumps(
                [store.reference_key(item.subject), item.relation,
                 store.reference_key(item.object),
                 item.qualifiers],
                ensure_ascii=False, sort_keys=True)
            claims.setdefault(key, item)
        for item in batch.next_reading_targets:
            targets.setdefault(item.query.casefold(), item)
        rejected.extend(batch.rejected)
    return ObservationBatch(
        entities=tuple(entities.values()),
        claims=tuple(claims.values()),
        next_reading_targets=tuple(targets.values()),
        rejected=tuple(rejected))


def _split_paragraph(paragraph: str, limit: int) -> list[str]:
    """整段就超过上限时按句子断开；仍然不从句子中间切。"""
    parts: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[。！？；!?;])", paragraph):
        if not sentence:
            continue
        current += sentence
        if len(current) >= limit:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return parts or [paragraph]


def split_text(text: str, limit: int = 12000) -> list[str]:
    """按自然段分块。上限是目标，不是硬边界。

    一段跨过上限时在这一段的**后面**断开，不在前面——取长的那一侧。在前面断会
    留下一个刚好卡在上限的短块，而抽取质量对上下文完整度更敏感，不对块长敏感。

    永远不在段落中间切。整段就超过上限的退一步按句子断（当前语料没有这种段落，
    最长 6401 字），这条路只是防止将来某个畸形输入把一整节拖垮。
    """
    units: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        units.extend(
            _split_paragraph(paragraph, limit) if len(paragraph) > limit
            else [paragraph])
    chunks: list[str] = []
    current = ""
    for unit in units:
        current = f"{current}\n\n{unit}" if current else unit
        if len(current) >= limit:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks or [text]
