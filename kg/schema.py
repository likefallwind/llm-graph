"""统一知识核心 schema 的安装入口。"""
from __future__ import annotations

from pathlib import Path
import time


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def ensure(conn) -> None:
    """安装增量 schema，并把机器可读关系注册表同步到数据库。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate_entity_resolution(conn)
    from .ontology import registry
    registry().sync(conn)
    from . import coverage
    coverage.sync(conn)
    conn.commit()


def _migrate_entity_resolution(conn) -> None:
    """为已有数据库补齐可审计的别名与实体消歧字段。"""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(aliases)")
    }
    if "status" not in columns:
        # 旧别名此前已参与自动匹配；迁移时保留其既有语义。
        conn.execute(
            "ALTER TABLE aliases ADD COLUMN status TEXT NOT NULL DEFAULT 'verified'")
    if "evidence_excerpt" not in columns:
        conn.execute(
            "ALTER TABLE aliases ADD COLUMN evidence_excerpt TEXT NOT NULL DEFAULT ''")
    event_columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(entity_resolution_events)")
    }
    if "llm_normalized_name" not in event_columns:
        conn.execute(
            "ALTER TABLE entity_resolution_events"
            " ADD COLUMN llm_normalized_name TEXT NOT NULL DEFAULT ''")
    if "selected_candidate_id" not in event_columns:
        conn.execute(
            "ALTER TABLE entity_resolution_events"
            " ADD COLUMN selected_candidate_id INTEGER REFERENCES entities(id)")
    # 与 canonical 完全同名的 alias 没有召回价值，保留记录但退出匹配。
    conn.execute(
        "UPDATE aliases SET status='rejected'"
        " WHERE status!='rejected' AND EXISTS ("
        " SELECT 1 FROM entities e"
        " WHERE e.id=aliases.entity_id"
        " AND e.normalized_name=aliases.normalized_name)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_aliases_resolution"
        " ON aliases(normalized_name, status)")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at)"
        " VALUES (5,'auditable_entity_resolution',?)",
        (time.time(),))
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at)"
        " VALUES (6,'accumulating_entity_alignment',?)",
        (time.time(),))
