"""统一知识核心 schema 的安装入口。"""
from __future__ import annotations

import json
from pathlib import Path
import time


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def ensure(conn) -> None:
    """安装增量 schema，并把机器可读关系注册表同步到数据库。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate_entity_resolution(conn)
    _migrate_entailment_reviews(conn)
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


def _migrate_entailment_reviews(conn) -> None:
    """为已有数据库补齐蕴含判定的历史记录与裁决快照字段。"""
    evidence_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(evidence)")
    }
    if "current_entailment_review_id" not in evidence_columns:
        conn.execute(
            "ALTER TABLE evidence ADD COLUMN current_entailment_review_id"
            " INTEGER REFERENCES entailment_reviews(id)")
    decision_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(decisions)")
    }
    if "evidence_review_snapshot" not in decision_columns:
        conn.execute(
            "ALTER TABLE decisions ADD COLUMN evidence_review_snapshot"
            " TEXT NOT NULL DEFAULT '[]'")
    # 留痕机制之前就存在的 verdict 补建 review 行。run_id 留空是诚实的表述：
    # 这些判定的 prompt 与模型版本已经不可考，不能伪造一个版本号。
    now = time.time()
    legacy = conn.execute(
        "SELECT id, entailment, metadata FROM evidence"
        " WHERE entailment!='unreviewed' AND current_entailment_review_id IS NULL"
    ).fetchall()
    for row in legacy:
        try:
            reason = json.loads(row["metadata"]).get("entailment_reason", "")
        except (TypeError, ValueError):
            reason = ""
        cur = conn.execute(
            "INSERT INTO entailment_reviews"
            " (evidence_id,run_id,verdict,reason,raw_output,created_at)"
            " VALUES (?,NULL,?,?,?,?)",
            (row["id"], row["entailment"], reason,
             json.dumps({"provenance": "pre_versioning_backfill"},
                        ensure_ascii=False, sort_keys=True), now))
        conn.execute(
            "UPDATE evidence SET current_entailment_review_id=? WHERE id=?",
            (cur.lastrowid, row["id"]))
    # 版本 7 已被 schema.sql 的 model_queue_reviews 占用。
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at)"
        " VALUES (8,'append_only_entailment_reviews',?)",
        (time.time(),))
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at)"
        " VALUES (9,'targeting_probes',?)",
        (time.time(),))
    merge_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(merge_events)")
    }
    if "payload" not in merge_columns:
        # 记下合并搬动了哪些行，撤销时才有依据。
        conn.execute(
            "ALTER TABLE merge_events ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at)"
        " VALUES (10,'reversible_entity_merge',?)",
        (time.time(),))
