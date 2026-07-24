"""把旧 nodes/edges 幂等迁移为 Entity/Claim/Evidence；不自动发布。"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import corpus, docs, observations, store


RELATED_MAP = {
    "同题替代": ("alternative_to", False, "explicit_comparison"),
    "演化启发": ("derived_from", True, "explicit_derivation"),
    "教学对比": ("pedagogical_contrast_with", False, "explicit_comparison"),
}


def preview(conn) -> dict:
    return {
        "legacy_nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        "legacy_edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "mapped_nodes": conn.execute("SELECT COUNT(*) FROM legacy_entity_map").fetchone()[0],
        "mapped_edges": conn.execute("SELECT COUNT(*) FROM legacy_claim_map").fetchone()[0],
        "migration_issues": conn.execute("SELECT COUNT(*) FROM migration_issues").fetchone()[0],
    }


def _authority(source_type: str) -> dict:
    if source_type == "textbook":
        from .pipeline import TEXTBOOK_AUTHORITY
        return TEXTBOOK_AUTHORITY
    return {}


def _snapshot(conn, source: str):
    source = source or "legacy:unknown"
    content = ""
    uri = ""
    storage_ref = ""
    language = ""
    source_type = "legacy"
    independence_group = source.split(":", 1)[0]
    source_slug = "legacy-" + hashlib.sha256(source.encode()).hexdigest()[:12]
    source_name = source
    version = source
    metadata = {"legacy_source": source}

    wiki_match = re.match(r"^wiki:(zh|en):(.+)@(\d+)$", source)
    doc_source = docs.parse_source(source)
    if wiki_match:
        language, title, revision = wiki_match.groups()
        page = corpus.get_page(conn, language, title)
        source_slug = f"wikipedia-{language}"
        source_name = f"Wikipedia {language}"
        source_type = "encyclopedia"
        independence_group = "wikipedia"
        version = f"{title}@{revision}"
        if page:
            content = page["text"]
            uri = corpus.url_of(page)
            storage_ref = f"corpus:{page['id']}"
            metadata.update({"page_id": page["page_id"], "title": page["title"]})
    elif doc_source:
        book, sec_id, content_hash = doc_source
        sec = docs.get_section(conn, book, sec_id)
        source_slug = f"doc-{book}"
        source_name = book
        source_type = "textbook"
        independence_group = f"book:{book}"
        version = f"{sec_id}@{content_hash}"
        if sec:
            content = sec["text"] or sec["orig_text"]
            language = "zh" if sec["text"] else sec["orig_lang"]
            uri = docs.url_of(sec)
            storage_ref = f"doc_sections:{sec['id']}"
            metadata.update({"book": book, "sec_id": sec_id, "title": sec["title"]})

    source_id = store.upsert_source(
        conn, source_slug, source_name, source_type,
        independence_group=independence_group,
        authority_profile=_authority(source_type), metadata=metadata)
    digest = hashlib.sha256((source + "\0" + content).encode()).hexdigest()
    return store.add_source_snapshot(
        conn, source_id, version, content=content, content_hash=digest,
        uri=uri, original_language=language, storage_ref=storage_ref,
        metadata=metadata)


def _strip_prefixes(rationale: str) -> tuple[list[str], str]:
    prefixes = []
    text = rationale or ""
    while True:
        match = re.match(r"^\[([^\]]+)\]\s*", text)
        if not match:
            break
        prefixes.append(match.group(1))
        text = text[match.end():]
    return prefixes, text.strip()


def _relation(edge: dict) -> tuple[str, bool, str] | None:
    if edge["type"] == "is_a":
        return "is_a", False, "explicit_taxonomy"
    if edge["type"] == "part_of":
        return "part_of", False, "explicit_composition"
    if edge["type"] == "prerequisite_of":
        prefixes, _ = _strip_prefixes(edge["rationale"])
        evidence_type = "derived_prerequisite" if "推断" in prefixes else "explicit_prerequisite"
        return "prerequisite_of", False, evidence_type
    if edge["type"] == "related_to":
        prefixes, _ = _strip_prefixes(edge["rationale"])
        for prefix in prefixes:
            if prefix in RELATED_MAP:
                return RELATED_MAP[prefix]
    return None


def _issue(conn, item_type: str, item_id: int, reason: str, payload: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO migration_issues"
        " (legacy_item_type,legacy_item_id,reason,payload,status,created_at)"
        " VALUES (?,?,?,?,'pending',?)",
        (item_type, item_id, reason,
         json.dumps(payload, ensure_ascii=False, sort_keys=True), time.time()))
    conn.commit()


def apply(conn) -> dict:
    result = {
        "entities": 0, "claims": 0, "evidence": 0,
        "skipped": 0, "warnings": [],
    }
    node_map = {
        row["legacy_node_id"]: row["entity_id"]
        for row in conn.execute("SELECT * FROM legacy_entity_map")}

    for node in conn.execute("SELECT * FROM nodes ORDER BY id"):
        if node["id"] in node_map:
            continue
        hits = store.find_entities(conn, node["name"])
        if len(hits) == 1:
            entity = hits[0]
        elif len(hits) > 1:
            result["warnings"].append(
                f"节点 {node['id']}「{node['name']}」命中多个实体，跳过")
            result["skipped"] += 1
            continue
        else:
            metadata = {
                "legacy_node_id": node["id"],
                "legacy_status": node["status"],
                "legacy_source": node["source"],
                "legacy_facets": json.loads(node["facets"]),
            }
            entity = store.add_entity(
                conn, node["name"], "concept", definition=node["definition"],
                status="rejected" if node["status"] == "rejected" else "proposed",
                metadata=metadata)
        for alias in json.loads(node["aliases"]):
            store.add_alias(conn, entity.id, alias, status="verified")
        node_map[node["id"]] = entity.id
        conn.execute(
            "INSERT INTO legacy_entity_map(legacy_node_id,entity_id,migrated_at)"
            " VALUES (?,?,?)", (node["id"], entity.id, time.time()))
        if node["definition"]:
            snapshot = _snapshot(conn, node["source"])
            mechanically_valid = observations.evidence_in_text(
                node["definition"], snapshot.content)
            store.add_evidence(
                conn, snapshot.id, node["definition"], "legacy_definition",
                entity_id=entity.id, mechanically_valid=mechanically_valid,
                metadata={"legacy_node_id": node["id"]})
            result["evidence"] += 1
        result["entities"] += 1
        conn.commit()

    mapped_edges = {
        row["legacy_edge_id"] for row in conn.execute("SELECT legacy_edge_id FROM legacy_claim_map")}
    issue_edges = {
        row["legacy_item_id"] for row in conn.execute(
            "SELECT legacy_item_id FROM migration_issues"
            " WHERE legacy_item_type='edge' AND status='pending'")}
    for row in conn.execute("SELECT * FROM edges ORDER BY id"):
        edge = dict(row)
        if edge["id"] in mapped_edges or edge["id"] in issue_edges:
            continue
        mapped = _relation(edge)
        if not mapped:
            message = f"边 {edge['id']} 无法安全映射关系 {edge['type']}"
            _issue(conn, "edge", edge["id"], message, edge)
            result["warnings"].append(message)
            result["skipped"] += 1
            continue
        relation, reverse, evidence_type = mapped
        subject_id = node_map.get(edge["dst"] if reverse else edge["src"])
        object_id = node_map.get(edge["src"] if reverse else edge["dst"])
        if not subject_id or not object_id:
            message = f"边 {edge['id']} 端点未迁移"
            _issue(conn, "edge", edge["id"], message, edge)
            result["warnings"].append(message)
            result["skipped"] += 1
            continue
        prefixes, excerpt = _strip_prefixes(edge["rationale"])
        try:
            claim = store.add_claim(
                conn, subject_id, relation, object_id,
                status="rejected" if edge["status"] == "rejected" else "proposed",
                confidence=edge["confidence"],
                metadata={
                    "legacy_edge_id": edge["id"], "legacy_type": edge["type"],
                    "legacy_status": edge["status"], "legacy_prefixes": prefixes,
                })
        except ValueError as exc:
            message = f"边 {edge['id']}：{exc}"
            _issue(conn, "edge", edge["id"], message, edge)
            result["warnings"].append(message)
            result["skipped"] += 1
            continue
        if excerpt:
            snapshot = _snapshot(conn, edge["source"])
            mechanically_valid = observations.evidence_in_text(excerpt, snapshot.content)
            store.add_evidence(
                conn, snapshot.id, excerpt, evidence_type, claim_id=claim.id,
                mechanically_valid=mechanically_valid,
                metadata={"legacy_edge_id": edge["id"], "original_rationale": edge["rationale"]})
            result["evidence"] += 1
        conn.execute(
            "INSERT INTO legacy_claim_map(legacy_edge_id,claim_id,migrated_at)"
            " VALUES (?,?,?)", (edge["id"], claim.id, time.time()))
        conn.commit()
        result["claims"] += 1
    return result
