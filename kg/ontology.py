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
    "lifecycle", "family", "description", "subject_types", "object_types",
    "symmetric", "transitive", "acyclic", "accepted_evidence_types",
    "minimum_evidence", "validator_version",
)


class Registry:
    def __init__(self, data: dict):
        self.version = int(data["version"])
        self.entity_types = data["entity_types"]
        self.evidence_types = data["evidence_types"]
        self.relations = data["relations"]
        self._validate_definition()

    def _validate_definition(self) -> None:
        known = set(self.entity_types)
        known_evidence = set(self.evidence_types)
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
            unknown = (set(policy["subject_types"]) | set(policy["object_types"])) - known
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

    def is_strong_evidence(self, relation: str, evidence_type: str) -> bool:
        """该证据类型能否作为这个关系的强支持。

        强弱是**按关系**判定的，不是全局的：explicit_function 对 used_for 是强
        证据，对 part_of 则恰好是 description 排除的语义。
        """
        return evidence_type in set(self.relation(relation)["accepted_evidence_types"])

    def validator_versions(self) -> dict[str, str]:
        return {name: policy["validator_version"]
                for name, policy in self.relations.items()}

    def validate_entity_type(self, entity_type: str) -> None:
        if entity_type not in self.entity_types:
            raise OntologyError(f"未知实体类型: {entity_type}")

    def relation(self, name: str) -> dict:
        try:
            return self.relations[name]
        except KeyError as exc:
            raise OntologyError(f"未知关系: {name}") from exc

    def validate_claim(self, subject_type: str, relation: str, object_type: str,
                       *, active_only: bool = False) -> None:
        self.validate_entity_type(subject_type)
        self.validate_entity_type(object_type)
        policy = self.relation(relation)
        if active_only and policy["lifecycle"] != "core":
            raise OntologyError(f"关系 {relation} 不在默认抽取的核心关系中")
        if subject_type not in policy["subject_types"]:
            raise OntologyError(
                f"关系 {relation} 不允许 subject 类型 {subject_type}")
        if object_type not in policy["object_types"]:
            raise OntologyError(
                f"关系 {relation} 不允许 object 类型 {object_type}")

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
