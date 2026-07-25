"""Entity/Claim/Evidence/Decision 核心存储 API。"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from typing import Any

from . import models
from .ontology import registry


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str) -> Any:
    return json.loads(value)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", value)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _entity(row) -> models.Entity:
    return models.Entity(
        id=row["id"], canonical_name=row["canonical_name"],
        normalized_name=row["normalized_name"], entity_type=row["entity_type"],
        definition=row["definition"], status=row["status"],
        metadata=_load(row["metadata"]))


def _claim(row) -> models.Claim:
    return models.Claim(
        id=row["id"], subject_id=row["subject_id"], relation=row["relation"],
        object_id=row["object_id"], qualifiers=_load(row["qualifiers"]),
        status=row["status"], confidence=row["confidence"],
        metadata=_load(row["metadata"]))


def _evidence(row) -> models.Evidence:
    return models.Evidence(
        id=row["id"], entity_id=row["entity_id"], claim_id=row["claim_id"],
        source_snapshot_id=row["source_snapshot_id"], polarity=row["polarity"],
        evidence_type=row["evidence_type"], excerpt=row["excerpt"],
        location=row["location"], mechanically_valid=bool(row["mechanically_valid"]),
        entailment=row["entailment"], metadata=_load(row["metadata"]),
        current_entailment_review_id=row["current_entailment_review_id"])


def create_run(conn, run_type: str, algorithm_version: str, *, model: str = "",
               prompt_version: str = "", config: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs(run_type,algorithm_version,model,prompt_version,config,status,started_at)"
        " VALUES (?,?,?,?,?,'running',?)",
        (run_type, algorithm_version, model, prompt_version, _json(config or {}), time.time()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str = "completed") -> None:
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError(f"非法运行状态: {status}")
    conn.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?",
                 (status, time.time(), run_id))
    conn.commit()


def upsert_source(conn, slug: str, name: str, source_type: str, *,
                  independence_group: str, authority_profile: dict | None = None,
                  metadata: dict | None = None) -> int:
    now = time.time()
    conn.execute(
        "INSERT INTO sources"
        " (slug,name,source_type,authority_profile,independence_group,metadata,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(slug) DO UPDATE SET name=excluded.name,"
        " source_type=excluded.source_type,"
        " authority_profile=excluded.authority_profile,"
        " independence_group=excluded.independence_group,"
        " metadata=excluded.metadata, updated_at=excluded.updated_at",
        (slug, name, source_type, _json(authority_profile or {}),
         independence_group, _json(metadata or {}), now, now))
    row = conn.execute("SELECT id FROM sources WHERE slug=?", (slug,)).fetchone()
    conn.commit()
    return row["id"]


def add_source_snapshot(conn, source_id: int, version: str, *, content: str = "",
                        content_hash: str = "", uri: str = "",
                        original_language: str = "", storage_ref: str = "",
                        metadata: dict | None = None) -> models.SourceSnapshot:
    digest = content_hash or _sha256(content)
    conn.execute(
        "INSERT OR IGNORE INTO source_snapshots"
        " (source_id,version,content_hash,uri,original_language,content,storage_ref,metadata,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (source_id, version, digest, uri, original_language, content, storage_ref,
         _json(metadata or {}), time.time()))
    row = conn.execute(
        "SELECT * FROM source_snapshots WHERE source_id=? AND content_hash=?",
        (source_id, digest)).fetchone()
    conn.commit()
    return models.SourceSnapshot(
        id=row["id"], source_id=row["source_id"], version=row["version"],
        content_hash=row["content_hash"], uri=row["uri"],
        original_language=row["original_language"], content=row["content"],
        storage_ref=row["storage_ref"], metadata=_load(row["metadata"]))


def add_entity(conn, canonical_name: str, entity_type: str, *, definition: str = "",
               status: str = "proposed", metadata: dict | None = None) -> models.Entity:
    registry().validate_entity_type(entity_type)
    normalized = normalize_name(canonical_name)
    if not normalized:
        raise ValueError("实体名称不能为空")
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO entities"
        " (canonical_name,normalized_name,entity_type,definition,status,metadata,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (canonical_name.strip(), normalized, entity_type, definition.strip(), status,
         _json(metadata or {}), now, now))
    row = conn.execute(
        "SELECT * FROM entities WHERE normalized_name=?", (normalized,)).fetchone()
    if row["entity_type"] != entity_type:
        raise ValueError(
            f"实体「{canonical_name}」已存在但类型为 {row['entity_type']}，不能改为 {entity_type}")
    conn.commit()
    return _entity(row)


def get_entity(conn, entity_id: int) -> models.Entity | None:
    row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    return _entity(row) if row else None


def add_alias(conn, entity_id: int, name: str, *, language: str = "",
              alias_type: str = "alias", source_snapshot_id: int | None = None,
              status: str = "proposed",
              evidence_excerpt: str = "") -> int | None:
    normalized = normalize_name(name)
    if not normalized:
        raise ValueError("别名不能为空")
    if status not in {"proposed", "verified", "rejected"}:
        raise ValueError(f"非法 alias 状态: {status}")
    entity = get_entity(conn, entity_id)
    if not entity:
        raise ValueError(f"实体不存在: {entity_id}")
    if normalized == entity.normalized_name:
        return None
    canonical = conn.execute(
        "SELECT id FROM entities WHERE normalized_name=? AND status!='rejected'",
        (normalized,)).fetchone()
    if canonical and canonical["id"] != entity_id:
        raise ValueError(
            f"别名「{name}」与实体 {canonical['id']} 的规范名冲突")
    conn.execute(
        "INSERT OR IGNORE INTO aliases"
        " (entity_id,name,normalized_name,language,alias_type,source_snapshot_id,"
        "  status,evidence_excerpt,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (entity_id, name.strip(), normalized, language, alias_type,
         source_snapshot_id, status, evidence_excerpt.strip(), time.time()))
    row = conn.execute(
        "SELECT id,status FROM aliases"
        " WHERE entity_id=? AND normalized_name=? AND language=?",
        (entity_id, normalized, language)).fetchone()
    if status == "verified" and row["status"] == "proposed":
        conn.execute(
            "UPDATE aliases SET status='verified',source_snapshot_id=COALESCE(?,source_snapshot_id),"
            " evidence_excerpt=CASE WHEN ?!='' THEN ? ELSE evidence_excerpt END WHERE id=?",
            (source_snapshot_id, evidence_excerpt.strip(), evidence_excerpt.strip(), row["id"]))
    conn.commit()
    return row["id"]


def set_alias_status(conn, alias_id: int, status: str) -> None:
    if status not in {"proposed", "verified", "rejected"}:
        raise ValueError(f"非法 alias 状态: {status}")
    cur = conn.execute("UPDATE aliases SET status=? WHERE id=?", (status, alias_id))
    if not cur.rowcount:
        raise ValueError(f"Alias 不存在: {alias_id}")
    conn.commit()


def update_alias_classification(conn, alias_id: int, *, status: str,
                                alias_type: str) -> None:
    if status not in {"proposed", "verified", "rejected"}:
        raise ValueError(f"非法 alias 状态: {status}")
    cur = conn.execute(
        "UPDATE aliases SET status=?,alias_type=? WHERE id=?",
        (status, alias_type, alias_id))
    if not cur.rowcount:
        raise ValueError(f"Alias 不存在: {alias_id}")
    conn.commit()


def add_model_queue_review(
        conn, *, queue_type: str, item_id: int, model: str, verdict: str,
        confidence: float, reason: str = "", payload: dict | None = None,
        policy_version: str) -> int:
    if queue_type not in {"entity_alignment", "type_conflict"}:
        raise ValueError(f"非法模型复核队列: {queue_type}")
    confidence = min(1.0, max(0.0, float(confidence)))
    cur = conn.execute(
        "INSERT INTO model_queue_reviews"
        " (queue_type,item_id,model,verdict,confidence,reason,payload,"
        "  policy_version,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (queue_type, item_id, model, verdict, confidence, reason,
         _json(payload or {}), policy_version, time.time()))
    conn.commit()
    return cur.lastrowid


def add_external_id(conn, entity_id: int, provider: str, external_id: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO entity_external_ids(entity_id,provider,external_id,created_at)"
        " VALUES (?,?,?,?)", (entity_id, provider, external_id, time.time()))
    row = conn.execute(
        "SELECT id,entity_id FROM entity_external_ids WHERE provider=? AND external_id=?",
        (provider, external_id)).fetchone()
    if row["entity_id"] != entity_id:
        raise ValueError(f"外部标识 {provider}:{external_id} 已属于其他实体")
    conn.commit()
    return row["id"]


def add_claim(conn, subject_id: int, relation: str, object_id: int, *,
              qualifiers: dict | None = None, status: str = "proposed",
              confidence: float | None = None,
              metadata: dict | None = None) -> models.Claim:
    if subject_id == object_id:
        raise ValueError("不允许实体指向自身的 Claim")
    subject, object_ = get_entity(conn, subject_id), get_entity(conn, object_id)
    if not subject or not object_:
        raise ValueError("Claim 的 subject 或 object 不存在")
    policy = registry().relation(relation)
    registry().validate_claim(subject.entity_type, relation, object_.entity_type)
    if policy["symmetric"] and subject_id > object_id:
        subject_id, object_id = object_id, subject_id
    packed = _json(qualifiers or {})
    digest = _sha256(packed)
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO claims"
        " (subject_id,relation,object_id,qualifiers,qualifiers_hash,status,confidence,metadata,"
        "  created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (subject_id, relation, object_id, packed, digest, status, confidence,
         _json(metadata or {}), now, now))
    row = conn.execute(
        "SELECT * FROM claims WHERE subject_id=? AND relation=? AND object_id=?"
        " AND qualifiers_hash=?", (subject_id, relation, object_id, digest)).fetchone()
    conn.commit()
    return _claim(row)


def get_claim(conn, claim_id: int) -> models.Claim | None:
    row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    return _claim(row) if row else None


def add_evidence(conn, source_snapshot_id: int, excerpt: str, evidence_type: str, *,
                 entity_id: int | None = None, claim_id: int | None = None,
                 polarity: str = "support", location: str = "",
                 mechanically_valid: bool = False, entailment: str = "unreviewed",
                 extraction_run_id: int | None = None,
                 metadata: dict | None = None) -> models.Evidence:
    if (entity_id is None) == (claim_id is None):
        raise ValueError("Evidence 必须且只能绑定一个 entity 或 claim")
    if polarity not in {"support", "oppose", "uncertain"}:
        raise ValueError(f"非法 evidence polarity: {polarity}")
    if entailment not in {"unreviewed", "supports", "contradicts", "insufficient"}:
        raise ValueError(f"非法 entailment: {entailment}")
    excerpt = excerpt.strip()
    if not excerpt:
        raise ValueError("Evidence excerpt 不能为空")
    target_key = f"entity:{entity_id}" if entity_id is not None else f"claim:{claim_id}"
    digest = _sha256(excerpt)
    conn.execute(
        "INSERT OR IGNORE INTO evidence"
        " (target_key,entity_id,claim_id,source_snapshot_id,polarity,evidence_type,excerpt,"
        "  excerpt_hash,location,mechanically_valid,entailment,extraction_run_id,metadata,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (target_key, entity_id, claim_id, source_snapshot_id, polarity, evidence_type,
         excerpt, digest, location, int(mechanically_valid), entailment,
         extraction_run_id, _json(metadata or {}), time.time()))
    row = conn.execute(
        "SELECT * FROM evidence WHERE target_key=? AND source_snapshot_id=?"
        " AND excerpt_hash=? AND polarity=?",
        (target_key, source_snapshot_id, digest, polarity)).fetchone()
    conn.commit()
    return _evidence(row)


def decide(conn, target_type: str, target_id: int, outcome: str, *,
           decided_by: str, policy_version: str = "", reason: str = "",
           evidence_ids: list[int] | None = None, batch_id: str = "",
           evidence_reviews: list[tuple[int, int | None]] | None = None) -> models.Decision:
    if target_type not in {"entity", "claim", "merge"}:
        raise ValueError(f"非法裁决目标: {target_type}")
    if decided_by not in {"human", "auto", "shadow"}:
        raise ValueError(f"非法裁决者: {decided_by}")
    allowed = {
        "approve", "reject", "auto_approve", "auto_reject",
        "needs_more_evidence", "human_review",
    }
    if outcome not in allowed:
        raise ValueError(f"非法裁决结果: {outcome}")
    target_key = f"{target_type}:{target_id}"
    evidence_snapshot = list(evidence_ids or [])
    review_snapshot = [
        {"evidence_id": evidence_id, "entailment_review_id": review_id}
        for evidence_id, review_id in (evidence_reviews or [])]
    cur = conn.execute(
        "INSERT INTO decisions"
        " (target_key,target_type,target_id,outcome,decided_by,policy_version,reason,"
        "  evidence_snapshot,evidence_review_snapshot,batch_id,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (target_key, target_type, target_id, outcome, decided_by, policy_version,
         reason, _json(evidence_snapshot), _json(review_snapshot), batch_id,
         time.time()))
    if decided_by != "shadow" and target_type in {"entity", "claim"}:
        status = {
            "approve": "published", "auto_approve": "published",
            "reject": "rejected", "auto_reject": "rejected",
            "needs_more_evidence": "needs_evidence",
        }.get(outcome)
        if status:
            table = "entities" if target_type == "entity" else "claims"
            if table == "entities" and status == "needs_evidence":
                status = "proposed"
            conn.execute(
                f"UPDATE {table} SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), target_id))
    conn.commit()
    return models.Decision(
        id=cur.lastrowid, target_type=target_type, target_id=target_id,
        outcome=outcome, decided_by=decided_by, policy_version=policy_version,
        reason=reason, evidence_snapshot=evidence_snapshot, batch_id=batch_id,
        evidence_review_snapshot=review_snapshot)


def identity_names(conn, entity_id: int) -> set[str]:
    """实体的身份名集合，全部是 normalized_name。

    只含 canonical 与 status='verified' 的别名。proposed 别名是待核的候选，
    不构成身份——正文里出现「SVM」不等于正文在讲「支持向量机」。
    """
    row = conn.execute(
        "SELECT normalized_name FROM entities WHERE id=?", (entity_id,)).fetchone()
    names = {row["normalized_name"]} if row else set()
    for alias in conn.execute(
            "SELECT normalized_name FROM aliases"
            " WHERE entity_id=? AND status='verified'", (entity_id,)):
        names.add(alias["normalized_name"])
    return {name for name in names if name}


def mentions_identity(excerpt: str, names: set[str]) -> bool:
    """正文里是否出现了这些身份名之一（两边都归一化后比对）。"""
    if not names:
        return False
    haystack = re.sub(r"\s+", "", normalize_name(excerpt))
    return any(re.sub(r"\s+", "", name) in haystack for name in names)


def evidence_endpoint_mentions(conn, evidence_row) -> dict:
    """这条 claim evidence 有没有按身份名提到两端。

    结果不入库：别名会从 proposed 转 verified，存下来的标记会过期，
    每次现算才反映当前的身份定义。
    """
    claim_id = evidence_row["claim_id"]
    if claim_id is None:
        return {}
    claim = conn.execute(
        "SELECT subject_id,object_id FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not claim:
        return {}
    excerpt = evidence_row["excerpt"]
    return {
        "subject": mentions_identity(
            excerpt, identity_names(conn, claim["subject_id"])),
        "object": mentions_identity(
            excerpt, identity_names(conn, claim["object_id"])),
    }


def find_canonical_entity(conn, name: str) -> models.Entity | None:
    """只查规范名。规范名全局唯一，是实体解析的最高优先级。"""
    normalized = normalize_name(name)
    row = conn.execute(
        "SELECT * FROM entities"
        " WHERE status!='rejected' AND normalized_name=?",
        (normalized,)).fetchone()
    return _entity(row) if row else None


def find_verified_alias_entities(conn, name: str) -> list[models.Entity]:
    """只通过已验证 alias 精确召回；同一 alias 可能仍有歧义。"""
    normalized = normalize_name(name)
    rows = conn.execute(
        "SELECT DISTINCT e.* FROM entities e"
        " JOIN aliases a ON a.entity_id=e.id"
        " WHERE e.status!='rejected'"
        " AND a.status='verified' AND a.normalized_name=?"
        " ORDER BY e.id",
        (normalized,)).fetchall()
    return [_entity(row) for row in rows]


def find_entities(conn, name: str) -> list[models.Entity]:
    """规范名优先；仅在规范名未命中时查询已验证 alias。"""
    canonical = find_canonical_entity(conn, name)
    return [canonical] if canonical else find_verified_alias_entities(conn, name)


def add_type_assertion(conn, entity_id: int, observed_type: str, *,
                       source_snapshot_id: int | None = None,
                       observation_id: int | None = None,
                       status: str, reason: str = "") -> int:
    if status not in {"consistent", "conflict"}:
        raise ValueError(f"非法类型断言状态: {status}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO entity_type_assertions"
        " (entity_id,observed_type,source_snapshot_id,observation_id,status,reason,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (entity_id, observed_type, source_snapshot_id, observation_id,
         status, reason, time.time()))
    conn.commit()
    return cur.lastrowid


def add_resolution_event(conn, *, raw_name: str, deterministic_name: str,
                         outcome: str, matched_by: str,
                         resolver_version: str, reason: str = "",
                         llm_normalized_name: str = "",
                         entity_id: int | None = None,
                         selected_candidate_id: int | None = None,
                         observation_id: int | None = None,
                         source_snapshot_id: int | None = None,
                         candidate_ids: list[int] | None = None,
                         confidence: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO entity_resolution_events"
        " (observation_id,source_snapshot_id,raw_name,deterministic_name,"
        "  llm_normalized_name,entity_id,selected_candidate_id,outcome,matched_by,"
        "  candidate_ids,confidence,reason,resolver_version,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, source_snapshot_id, raw_name, deterministic_name,
         llm_normalized_name, entity_id, selected_candidate_id, outcome,
         matched_by, _json(candidate_ids or []),
         confidence, reason, resolver_version, time.time()))
    conn.commit()
    return cur.lastrowid


def add_alignment_evidence(
        conn, *, observed_name: str, entity_id: int, confidence: float,
        policy_version: str, resolver_version: str, reason: str = "",
        source_snapshot_id: int | None = None,
        observation_id: int | None = None,
        direct_verify: bool = False) -> dict:
    """累计 suspected_same_entity 证据，并在跨来源门槛满足时验证 alias。"""
    normalized = normalize_name(observed_name)
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO entity_alignment_candidates"
        " (observed_name,normalized_name,entity_id,relation,status,score,"
        "  evidence_count,independent_sources,policy_version,created_at,updated_at)"
        " VALUES (?,?,?,'suspected_same_entity','suspected',0.0,0,0,?,?,?)",
        (observed_name.strip(), normalized, entity_id, policy_version, now, now))
    candidate = conn.execute(
        "SELECT * FROM entity_alignment_candidates"
        " WHERE normalized_name=? AND entity_id=?",
        (normalized, entity_id)).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO entity_alignment_evidence"
        " (candidate_id,source_snapshot_id,observation_id,confidence,reason,"
        "  resolver_version,created_at) VALUES (?,?,?,?,?,?,?)",
        (candidate["id"], source_snapshot_id, observation_id, confidence,
         reason, resolver_version, now))

    evidence_rows = conn.execute(
        "SELECT ae.confidence,s.independence_group"
        " FROM entity_alignment_evidence ae"
        " LEFT JOIN source_snapshots ss ON ss.id=ae.source_snapshot_id"
        " LEFT JOIN sources s ON s.id=ss.source_id"
        " WHERE ae.candidate_id=?",
        (candidate["id"],)).fetchall()
    by_group: dict[str, float] = {}
    for row in evidence_rows:
        group = row["independence_group"]
        if not group:
            # 无可溯源来源的证据不参与跨来源自动升级。
            continue
        by_group[group] = max(by_group.get(group, 0.0), row["confidence"])
    score = 0.0
    if by_group:
        remaining = 1.0
        for value in by_group.values():
            remaining *= 1.0 - value
        score = 1.0 - remaining
    count = len(evidence_rows)
    groups = len(by_group)
    conn.execute(
        "UPDATE entity_alignment_candidates"
        " SET score=?,evidence_count=?,independent_sources=?,updated_at=?"
        " WHERE id=?",
        (score, count, groups, now, candidate["id"]))

    competing = conn.execute(
        "SELECT COUNT(*) FROM entity_alignment_candidates"
        " WHERE normalized_name=? AND entity_id!=? AND status!='rejected'"
        " AND score>=?",
        (normalized, entity_id, max(0.0, score - 0.10))).fetchone()[0]
    if ((groups >= 2 and score >= 0.95) or direct_verify) and not competing:
        conn.execute(
            "UPDATE entity_alignment_candidates SET status='verified',updated_at=?"
            " WHERE id=?", (now, candidate["id"]))
        conn.execute(
            "UPDATE aliases SET status='verified'"
            " WHERE entity_id=? AND normalized_name=? AND status='proposed'",
            (entity_id, normalized))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM entity_alignment_candidates WHERE id=?",
        (candidate["id"],)).fetchone()
    return dict(row)


def add_observation(conn, run_id: int, source_snapshot_id: int, *,
                    subject_text: str = "", subject_type: str = "",
                    relation: str = "", object_text: str = "",
                    object_type: str = "", excerpt: str,
                    location: str = "", payload: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO observations"
        " (run_id,source_snapshot_id,subject_text,subject_type,relation,"
        "  object_text,object_type,excerpt,location,payload,status,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
        (run_id, source_snapshot_id, subject_text, subject_type, relation,
         object_text, object_type, excerpt, location, _json(payload or {}),
         time.time()))
    conn.commit()
    return cur.lastrowid


def resolve_observation(conn, observation_id: int, accepted: bool) -> None:
    conn.execute("UPDATE observations SET status=? WHERE id=?",
                 ("resolved" if accepted else "rejected", observation_id))
    conn.commit()


def evidence_for_claim(conn, claim_id: int) -> list[models.Evidence]:
    rows = conn.execute(
        "SELECT * FROM evidence WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
    return [_evidence(row) for row in rows]


ENTAILMENT_VERDICTS = ("supports", "contradicts", "insufficient")


def add_entailment_review(conn, evidence_id: int, verdict: str, *,
                          run_id: int | None = None, reason: str = "",
                          raw_output: dict | None = None) -> models.EntailmentReview:
    """追加一条蕴含判定记录，并把它设为该证据的当前结果。

    历史记录只增不改：同一条证据被重判多少次，就有多少行 review，
    evidence.entailment 只是最后一次的缓存。
    """
    if verdict not in set(ENTAILMENT_VERDICTS):
        raise ValueError(f"非法 entailment: {verdict}")
    row = conn.execute(
        "SELECT metadata FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if not row:
        raise ValueError(f"Evidence 不存在: {evidence_id}")
    cur = conn.execute(
        "INSERT INTO entailment_reviews"
        " (evidence_id,run_id,verdict,reason,raw_output,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (evidence_id, run_id, verdict, reason, _json(raw_output or {}), time.time()))
    review_id = cur.lastrowid
    metadata = _load(row["metadata"])
    if reason:
        metadata["entailment_reason"] = reason
    conn.execute(
        "UPDATE evidence SET entailment=?, current_entailment_review_id=?, metadata=?"
        " WHERE id=?", (verdict, review_id, _json(metadata), evidence_id))
    conn.commit()
    return models.EntailmentReview(
        id=review_id, evidence_id=evidence_id, run_id=run_id, verdict=verdict,
        reason=reason, raw_output=raw_output or {})


def entailment_reviews(conn, evidence_id: int) -> list[models.EntailmentReview]:
    """按时间正序返回某条证据的全部判定历史。"""
    rows = conn.execute(
        "SELECT * FROM entailment_reviews WHERE evidence_id=? ORDER BY id",
        (evidence_id,)).fetchall()
    return [models.EntailmentReview(
        id=row["id"], evidence_id=row["evidence_id"], run_id=row["run_id"],
        verdict=row["verdict"], reason=row["reason"],
        raw_output=_load(row["raw_output"])) for row in rows]


def update_entailment(conn, evidence_id: int, entailment: str,
                      *, reason: str = "") -> None:
    """兼容入口：等价于一次没有 run 归属的 add_entailment_review。"""
    add_entailment_review(conn, evidence_id, entailment, reason=reason)
