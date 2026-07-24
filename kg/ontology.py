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
            for field in ("family", "description", "subject_types", "object_types",
                          "symmetric", "transitive", "acyclic"):
                if field not in policy:
                    raise OntologyError(f"关系 {name} 缺少字段 {field}")
            unknown = (set(policy["subject_types"]) | set(policy["object_types"])) - known
            if unknown:
                raise OntologyError(f"关系 {name} 使用未知实体类型: {sorted(unknown)}")

    def validate_entity_type(self, entity_type: str) -> None:
        if entity_type not in self.entity_types:
            raise OntologyError(f"未知实体类型: {entity_type}")

    def relation(self, name: str) -> dict:
        try:
            return self.relations[name]
        except KeyError as exc:
            raise OntologyError(f"未知关系: {name}") from exc

    def validate_claim(self, subject_type: str, relation: str, object_type: str) -> None:
        self.validate_entity_type(subject_type)
        self.validate_entity_type(object_type)
        policy = self.relation(relation)
        if subject_type not in policy["subject_types"]:
            raise OntologyError(
                f"关系 {relation} 不允许 subject 类型 {subject_type}")
        if object_type not in policy["object_types"]:
            raise OntologyError(
                f"关系 {relation} 不允许 object 类型 {object_type}")

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
