"""保守实体消歧：精确命中自动复用，歧义绝不自动合并。"""
from __future__ import annotations

from dataclasses import dataclass

from . import store
from .observations import EntityObservation


@dataclass(frozen=True)
class Resolution:
    entity_id: int | None
    outcome: str
    reason: str


def resolve(conn, observation: EntityObservation) -> Resolution:
    hits = store.find_entities(conn, observation.name)
    if len(hits) > 1:
        return Resolution(None, "ambiguous", "名称或别名命中多个实体")
    if hits:
        hit = hits[0]
        if hit.entity_type != observation.entity_type:
            return Resolution(
                None, "ambiguous",
                f"已有实体类型 {hit.entity_type} 与观察类型 {observation.entity_type} 冲突")
        return Resolution(hit.id, "same_entity", "规范名或别名精确命中")
    entity = store.add_entity(
        conn, observation.name, observation.entity_type,
        definition=observation.definition, status="proposed",
        metadata={"created_from": "grounded_observation"})
    return Resolution(entity.id, "created", "有据 Observation 创建 proposed 实体")
