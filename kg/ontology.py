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


class Registry:
    def __init__(self, data: dict):
        self.version = int(data["version"])
        self.entity_types = data["entity_types"]
        self.relations = data["relations"]
        self._validate_definition()

    def _validate_definition(self) -> None:
        known = set(self.entity_types)
        for name, policy in self.relations.items():
            for field in ("lifecycle", "family", "description", "subject_types", "object_types",
                          "symmetric", "transitive", "acyclic"):
                if field not in policy:
                    raise OntologyError(f"关系 {name} 缺少字段 {field}")
            if policy["lifecycle"] not in {"core", "experimental"}:
                raise OntologyError(
                    f"关系 {name} lifecycle 非法: {policy['lifecycle']}")
            unknown = (set(policy["subject_types"]) | set(policy["object_types"])) - known
            if unknown:
                raise OntologyError(f"关系 {name} 使用未知实体类型: {sorted(unknown)}")
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
