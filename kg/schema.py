"""统一知识核心 schema 的安装入口。"""
from __future__ import annotations

from pathlib import Path


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def ensure(conn) -> None:
    """安装增量 schema，并把机器可读关系注册表同步到数据库。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    from .ontology import registry
    registry().sync(conn)
    conn.commit()
