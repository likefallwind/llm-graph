"""把 AI 覆盖分类配置同步到 reading-task 数据层。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml


TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "config" / "ai-coverage-taxonomy.yaml"


def sync(conn) -> None:
    data = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))
    now = time.time()

    def visit(node: dict, parent_id: str | None = None) -> None:
        policy = {
            key: value for key, value in node.items()
            if key not in {"id", "name", "children", "importance"}
        }
        conn.execute(
            "INSERT INTO coverage_topics(id,parent_id,name,importance,policy,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id,"
            " name=excluded.name,importance=excluded.importance,"
            " policy=excluded.policy,updated_at=excluded.updated_at",
            (node["id"], parent_id, node["name"], float(node.get("importance", 1.0)),
             json.dumps(policy, ensure_ascii=False, sort_keys=True), now, now))
        for child in node.get("children", []):
            visit(child, node["id"])

    visit(data["root"])


def exists(conn, topic_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM coverage_topics WHERE id=?", (topic_id,)).fetchone() is not None
