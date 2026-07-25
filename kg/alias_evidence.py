"""从语料里找显式的别名声明（零 LLM）。

`_validated_direct_match_type` 是充分条件：符合就能判定同名，不符合**不能**判定
不同名。此前不符合的别名没有任何转正通道，等于把充分条件当必要条件用了。

这里补上第二条通道。教材写作时会主动交代别名：

    支持向量机（Support Vector Machine，SVM）
    Logistic 回归也称为对数几率回归
    全连接层（fully-connected layer）或称为稠密层（dense layer）

这是语料给出的证据，不是模型判断。按证据政策，跨独立来源组的显式声明可以累计，
跨够来源组即转正；单个来源的一次声明不足以转正。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import local_corpus, store


# 每条语料声明的证据强度。累计公式是 1-Π(1-c)，取 0.9 时：
# 单个来源组 0.90 < 0.95 不转正，两个独立来源组 0.99 >= 0.95 转正。
DECLARATION_CONFIDENCE = 0.9

ALSO_MARKERS = (
    "又称为|又称|亦称为|亦称|也称为|也称|简称为|简称|又叫做|又叫|也叫做|也叫"
    "|或称为|或称|记作|缩写为|全称为|英文为|英文名为")

MAX_INNER = 40


@dataclass(frozen=True)
class Declaration:
    ref: str
    kind: str
    key: str
    independence_group: str
    pattern: str
    excerpt: str


def _patterns(alias: str, canonical: str) -> list[tuple[str, str]]:
    a, b = re.escape(alias), re.escape(canonical)
    inner = f"[^）)]{{0,{MAX_INNER}}}"
    clause = r"[^。；\n]{0,20}"
    return [
        (rf"{b}\s*[（(]{inner}{a}[^）)]{{0,10}}[）)]", "括号注释"),
        (rf"{a}\s*[（(]{inner}{b}[^）)]{{0,10}}[）)]", "括号注释"),
        (rf"{b}{clause}(?:{ALSO_MARKERS}){clause}{a}", "又称句式"),
        (rf"{a}{clause}(?:{ALSO_MARKERS}){clause}{b}", "又称句式"),
    ]


def find_declarations(conn, alias: str, canonical: str) -> list[Declaration]:
    """在本地语料里找同时点名两个名字的显式别名句式。"""
    if not alias.strip() or not canonical.strip():
        return []
    if store.normalize_name(alias) == store.normalize_name(canonical):
        return []
    compiled = [(re.compile(pattern), kind)
                for pattern, kind in _patterns(alias, canonical)]
    found = []
    for passage in local_corpus.passages(conn):
        for regex, kind in compiled:
            match = regex.search(passage.text)
            if not match:
                continue
            found.append(Declaration(
                ref=passage.ref, kind=passage.kind, key=passage.key,
                independence_group=passage.independence_group,
                pattern=kind, excerpt=match.group(0)[:200]))
            break
    return found


def independent_groups(declarations: list[Declaration]) -> set[str]:
    return {item.independence_group for item in declarations}


def record(conn, alias_id: int, *, policy_version: str, resolver_version: str,
           declarations: list[Declaration] | None = None) -> dict:
    """把语料声明登记成对齐证据，跨够独立来源组时由累计逻辑自动转正。"""
    row = conn.execute(
        "SELECT a.id,a.name,a.entity_id,a.status,e.canonical_name"
        " FROM aliases a JOIN entities e ON e.id=a.entity_id WHERE a.id=?",
        (alias_id,)).fetchone()
    if not row:
        raise ValueError(f"别名不存在: {alias_id}")
    if declarations is None:
        declarations = find_declarations(conn, row["name"], row["canonical_name"])
    if not declarations:
        return {
            "alias_id": alias_id, "alias": row["name"],
            "entity": row["canonical_name"], "declarations": 0,
            "independent_groups": 0, "status": row["status"],
        }
    # 同一来源组只取一条：一本书重复写十次也只是一个来源。
    by_group: dict[str, Declaration] = {}
    for item in declarations:
        by_group.setdefault(item.independence_group, item)
    texts = {passage.ref: passage.text for passage in local_corpus.passages(conn)}
    alignment = None
    for item in by_group.values():
        snapshot = local_corpus.snapshot_for(
            conn, item.kind, item.key, texts[item.ref])
        alignment = store.add_alignment_evidence(
            conn, observed_name=row["name"], entity_id=row["entity_id"],
            confidence=DECLARATION_CONFIDENCE, policy_version=policy_version,
            resolver_version=resolver_version,
            reason=f"[语料声明/{item.pattern}] {item.ref}: {item.excerpt[:120]}",
            source_snapshot_id=snapshot.id)
    status = conn.execute(
        "SELECT status FROM aliases WHERE id=?", (alias_id,)).fetchone()["status"]
    return {
        "alias_id": alias_id, "alias": row["name"],
        "entity": row["canonical_name"],
        "declarations": len(declarations),
        "independent_groups": len(by_group),
        "groups": sorted(by_group),
        "alignment_score": round(alignment["score"], 3) if alignment else 0.0,
        "status": status,
        "examples": [
            {"ref": item.ref, "pattern": item.pattern, "excerpt": item.excerpt}
            for item in list(by_group.values())[:3]],
    }


def scan(conn, *, status: str = "proposed", limit: int | None = None) -> dict:
    """只读扫描：哪些别名能在语料里找到显式声明。"""
    rows = conn.execute(
        "SELECT a.id,a.name,a.alias_type,a.status,e.canonical_name"
        " FROM aliases a JOIN entities e ON e.id=a.entity_id"
        " WHERE a.status=? ORDER BY a.id" + (" LIMIT ?" if limit else ""),
        (status, limit) if limit else (status,)).fetchall()
    items = []
    for row in rows:
        declarations = find_declarations(
            conn, row["name"], row["canonical_name"])
        if not declarations:
            continue
        groups = independent_groups(declarations)
        items.append({
            "alias_id": row["id"], "alias": row["name"],
            "entity": row["canonical_name"], "alias_type": row["alias_type"],
            "independent_groups": sorted(groups),
            "would_verify": len(groups) >= 2,
            "examples": [
                {"ref": item.ref, "pattern": item.pattern,
                 "excerpt": item.excerpt} for item in declarations[:3]],
        })
    return {
        "examined": len(rows),
        "with_declarations": len(items),
        "would_verify": sum(1 for item in items if item["would_verify"]),
        "items": items,
    }
