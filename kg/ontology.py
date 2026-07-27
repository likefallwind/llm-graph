"""加载并执行实体与关系本体约束。"""
from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path

import yaml


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "relation-registry.yaml"


class OntologyError(ValueError):
    pass


REQUIRED_RELATION_FIELDS = (
    "lifecycle", "family", "description",
    "typical_subject_types", "typical_object_types",
    "symmetric", "transitive", "acyclic", "accepted_evidence_types",
    "minimum_evidence", "validator_version",
)


class Registry:
    def __init__(self, data: dict):
        self.version = int(data["version"])
        self.entity_types = data["entity_types"]
        self.entity_type_disambiguation = list(data.get("disambiguation", []))
        self.evidence_types = data["evidence_types"]
        self.relations = data["relations"]
        self._validate_definition()

    def _validate_definition(self) -> None:
        known = set(self.entity_types)
        known_evidence = set(self.evidence_types)
        # 判据由这些字段生成并注入提示词。缺一个就等于让模型凭字面猜类型，
        # 那正是六类改造要消灭的失败模式，所以在加载期就失败。
        priorities = []
        for name, spec in self.entity_types.items():
            for field in ("priority", "description", "positive", "negative"):
                if not spec.get(field):
                    raise OntologyError(f"实体类型 {name} 缺少字段 {field}")
            priorities.append(spec["priority"])
        if sorted(priorities) != list(range(1, len(known) + 1)):
            raise OntologyError(
                f"实体类型 priority 必须是 1..{len(known)} 的排列: {sorted(priorities)}")
        if not self.entity_type_disambiguation:
            raise OntologyError("缺少 disambiguation 消歧从句")
        for name, spec in self.evidence_types.items():
            if spec.get("strength") not in {"strong", "weak"}:
                raise OntologyError(
                    f"证据类型 {name} 的 strength 非法: {spec.get('strength')}")
        for name, policy in self.relations.items():
            for field in REQUIRED_RELATION_FIELDS:
                if field not in policy:
                    raise OntologyError(f"关系 {name} 缺少字段 {field}")
            if policy["lifecycle"] not in {"core", "experimental"}:
                raise OntologyError(
                    f"关系 {name} lifecycle 非法: {policy['lifecycle']}")
            unknown = (set(policy["typical_subject_types"])
                       | set(policy["typical_object_types"])) - known
            if unknown:
                raise OntologyError(f"关系 {name} 使用未知实体类型: {sorted(unknown)}")
            accepted = set(policy["accepted_evidence_types"])
            unknown_evidence = accepted - known_evidence
            if unknown_evidence:
                raise OntologyError(
                    f"关系 {name} 使用未知证据类型: {sorted(unknown_evidence)}")
            weak = {item for item in accepted
                    if self.evidence_types[item]["strength"] != "strong"}
            if weak:
                raise OntologyError(
                    f"关系 {name} 把弱证据列为可接受: {sorted(weak)}")
            qualifiers = set(policy.get("qualifiers", []))
            required = set(policy.get("required_qualifiers", []))
            value_keys = set(policy.get("qualifier_values", {}))
            if required - qualifiers:
                raise OntologyError(
                    f"关系 {name} 的 required_qualifiers 未声明: "
                    f"{sorted(required - qualifiers)}")
            if value_keys - qualifiers:
                raise OntologyError(
                    f"关系 {name} 的 qualifier_values 未声明: "
                    f"{sorted(value_keys - qualifiers)}")

    @property
    def active_relations(self) -> dict[str, dict]:
        """默认抽取允许使用的核心关系。"""
        return {
            name: policy for name, policy in self.relations.items()
            if policy["lifecycle"] == "core"
        }

    def evidence_type_names(self) -> list[str]:
        """抽取器允许输出的证据类型；提示词的可选值从这里生成。"""
        return list(self.evidence_types)

    def validate_evidence_type(self, evidence_type: str) -> None:
        if evidence_type not in self.evidence_types:
            raise OntologyError(f"未知证据类型: {evidence_type}")

    def is_assertive_evidence(self, evidence_type: str) -> bool:
        """这个证据类型本身是不是一句断言。

        全局属性，和关系无关。目录序、超链接、共现是编排不是断言——它们既不能
        支持一个关系，也不能反驳一个关系。
        """
        self.validate_evidence_type(evidence_type)
        return self.evidence_types[evidence_type]["strength"] == "strong"

    def is_strong_evidence(self, relation: str, evidence_type: str) -> bool:
        """该证据类型能否**建立**这个关系。

        按关系判定：explicit_function 对 used_for 是强证据，对 part_of 则恰好是
        description 排除的语义。

        注意这只管「能不能建立」。一条断言不在白名单里，不代表它不能反驳该关系
        ——「特征是预测所依据的自变量」这个定义建立不了 part_of，但它确实是对
        `特征 part_of 样本` 的有效反驳。反驳看的是 is_assertive_evidence。
        """
        return evidence_type in set(self.relation(relation)["accepted_evidence_types"])

    def validator_versions(self) -> dict[str, str]:
        return {name: policy["validator_version"]
                for name, policy in self.relations.items()}

    def validate_entity_type(self, entity_type: str) -> None:
        if entity_type not in self.entity_types:
            raise OntologyError(f"未知实体类型: {entity_type}")

    def ordered_entity_types(self) -> list[str]:
        """按 priority 升序的主类型；没有 priority 时退回声明顺序。"""
        return sorted(
            self.entity_types,
            key=lambda name: self.entity_types[name].get("priority", 999))

    def entity_type_contract(self) -> str:
        """生成给抽取和复核模型看的主类型判据。

        判据只有一份，就在注册表里。提示词从这里生成而不是各自抄一遍——第二份
        词表会被 tests/test_core_isolation.py 抓到，而且抄出来的两份迟早会分叉。

        没有 priority/positive/negative 的旧词表退化成只列类型名，与改造前一致。
        """
        lines = []
        for index, name in enumerate(self.ordered_entity_types(), start=1):
            spec = self.entity_types[name]
            lines.append(f"{index}. {name} —— {spec['description']}")
            if spec.get("positive"):
                lines.append(f"   正例：{'、'.join(spec['positive'])}")
            for item in spec.get("negative", []):
                lines.append(f"   反例：{item}")
        if self.entity_type_disambiguation:
            lines.append("")
            lines.append("补充规则：")
            lines.extend(f"- {item}" for item in self.entity_type_disambiguation)
        return "\n".join(lines)

    def relation(self, name: str) -> dict:
        try:
            return self.relations[name]
        except KeyError as exc:
            raise OntologyError(f"未知关系: {name}") from exc

    def validate_claim(self, subject_type: str, relation: str, object_type: str,
                       *, active_only: bool = False) -> None:
        self.validate_claim_endpoint_types(
            subject_type, relation, object_type, active_only=active_only)

    def validate_claim_endpoint_types(
            self, subject_type: str | None, relation: str,
            object_type: str | None, *, active_only: bool = False) -> None:
        """校验关系存在、可用，以及端点类型本身合法。

        **端点类型不再是硬闸。** 主类型表达的是实体的规范身份，而关系问的是它在
        这句话里承担什么角色——拿身份去闸角色必然误伤，而且和"主类型不声称表达
        全部用途"这条自相矛盾。真实反例：

            正则化 solves 过拟合          过拟合是 concept，不是 task
            模型 evaluated_by ImageNet    ImageNet 是 data，不是 criterion
            语言模型 trained_on 某教材     教材是 resource，不是 data

        三句都成立，旧规则三句都拒。关系成不成立回到证据和蕴含验证去判；端点
        是否典型改由 `atypical_endpoints` 报告，进复核队列而不是被静默丢弃。
        """
        policy = self.relation(relation)
        if active_only and policy["lifecycle"] != "core":
            raise OntologyError(f"关系 {relation} 不在默认抽取的核心关系中")
        if subject_type is not None:
            self.validate_entity_type(subject_type)
        if object_type is not None:
            self.validate_entity_type(object_type)

    def atypical_endpoints(self, subject_type: str | None, relation: str,
                           object_type: str | None) -> list[str]:
        """端点类型是否落在注册表列出的典型范围外。只报告，不拒收。"""
        policy = self.relation(relation)
        out = []
        if (subject_type is not None
                and subject_type not in policy["typical_subject_types"]):
            out.append(
                f"subject 类型 {subject_type} 不在 {relation} 的典型范围"
                f" {policy['typical_subject_types']}")
        if (object_type is not None
                and object_type not in policy["typical_object_types"]):
            out.append(
                f"object 类型 {object_type} 不在 {relation} 的典型范围"
                f" {policy['typical_object_types']}")
        return out

    def validate_qualifiers(self, relation: str, qualifiers: dict,
                            *, require_required: bool = False) -> None:
        policy = self.relation(relation)
        allowed = set(policy.get("qualifiers", []))
        unknown = set(qualifiers) - allowed
        if unknown:
            raise OntologyError(
                f"关系 {relation} 使用未知 qualifiers: {sorted(unknown)}")
        if require_required:
            missing = set(policy.get("required_qualifiers", [])) - set(qualifiers)
            if missing:
                raise OntologyError(
                    f"关系 {relation} 缺少必填 qualifiers: {sorted(missing)}")
        for key, allowed_values in policy.get("qualifier_values", {}).items():
            if key in qualifiers and qualifiers[key] not in allowed_values:
                raise OntologyError(
                    f"关系 {relation} 的 qualifier {key} 值非法: "
                    f"{qualifiers[key]!r}")

    def extraction_contract(self) -> str:
        """生成给抽取模型看的、稳定且可审计的核心关系契约。"""
        contracts = {}
        for name, policy in self.active_relations.items():
            item = {"description": policy["description"]}
            # 典型端点是**引导**不是限制：写进契约让模型知道这条关系一般连什么，
            # 但端点类型不合典型不构成拒收理由——见 validate_claim_endpoint_types。
            item["typical_subject_types"] = policy["typical_subject_types"]
            item["typical_object_types"] = policy["typical_object_types"]
            if policy.get("qualifiers"):
                item["qualifiers"] = policy["qualifiers"]
            if policy.get("required_qualifiers"):
                item["required_qualifiers"] = policy["required_qualifiers"]
            if policy.get("qualifier_values"):
                item["qualifier_values"] = policy["qualifier_values"]
            contracts[name] = item
        return json.dumps(contracts, ensure_ascii=False, sort_keys=True)

    def sync(self, conn) -> None:
        now = time.time()
        for name, policy in self.relations.items():
            conn.execute(
                "INSERT INTO relation_definitions"
                " (name, registry_version, family, definition, policy, updated_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " registry_version=excluded.registry_version,"
                " family=excluded.family, definition=excluded.definition,"
                " policy=excluded.policy, updated_at=excluded.updated_at",
                (name, self.version, policy["family"], policy["description"],
                 json.dumps(policy, ensure_ascii=False, sort_keys=True), now))


@lru_cache(maxsize=1)
def registry() -> Registry:
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    return Registry(data)
