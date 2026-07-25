PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id                    INTEGER PRIMARY KEY,
    slug                  TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    source_type           TEXT NOT NULL,
    authority_profile     TEXT NOT NULL DEFAULT '{}',
    independence_group    TEXT NOT NULL,
    metadata              TEXT NOT NULL DEFAULT '{}',
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    id                 INTEGER PRIMARY KEY,
    source_id          INTEGER NOT NULL REFERENCES sources(id),
    version            TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    uri                TEXT NOT NULL DEFAULT '',
    original_language  TEXT NOT NULL DEFAULT '',
    content            TEXT NOT NULL DEFAULT '',
    storage_ref        TEXT NOT NULL DEFAULT '',
    metadata           TEXT NOT NULL DEFAULT '{}',
    created_at         REAL NOT NULL,
    UNIQUE(source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_source
    ON source_snapshots(source_id);

CREATE TABLE IF NOT EXISTS entities (
    id                INTEGER PRIMARY KEY,
    canonical_name    TEXT NOT NULL,
    normalized_name   TEXT NOT NULL UNIQUE,
    entity_type       TEXT NOT NULL,
    definition        TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK(status IN ('proposed','published','rejected','merged')),
    embedding         TEXT,
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_type_status
    ON entities(entity_type, status);

CREATE TABLE IF NOT EXISTS aliases (
    id                INTEGER PRIMARY KEY,
    entity_id         INTEGER NOT NULL REFERENCES entities(id),
    name              TEXT NOT NULL,
    normalized_name   TEXT NOT NULL,
    language          TEXT NOT NULL DEFAULT '',
    alias_type        TEXT NOT NULL DEFAULT 'alias',
    source_snapshot_id INTEGER REFERENCES source_snapshots(id),
    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK(status IN ('proposed','verified','rejected')),
    evidence_excerpt  TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    UNIQUE(entity_id, normalized_name, language)
);
CREATE INDEX IF NOT EXISTS idx_aliases_normalized
    ON aliases(normalized_name);

CREATE TABLE IF NOT EXISTS entity_type_assertions (
    id                 INTEGER PRIMARY KEY,
    entity_id          INTEGER NOT NULL REFERENCES entities(id),
    observed_type      TEXT NOT NULL,
    source_snapshot_id INTEGER REFERENCES source_snapshots(id),
    observation_id     INTEGER REFERENCES observations(id),
    status             TEXT NOT NULL
                       CHECK(status IN ('consistent','conflict')),
    reason             TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    UNIQUE(observation_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_type_assertions_entity
    ON entity_type_assertions(entity_id, status);

CREATE TABLE IF NOT EXISTS entity_resolution_events (
    id                 INTEGER PRIMARY KEY,
    observation_id     INTEGER REFERENCES observations(id),
    source_snapshot_id INTEGER REFERENCES source_snapshots(id),
    raw_name           TEXT NOT NULL,
    deterministic_name TEXT NOT NULL,
    llm_normalized_name TEXT NOT NULL DEFAULT '',
    entity_id          INTEGER REFERENCES entities(id),
    selected_candidate_id INTEGER REFERENCES entities(id),
    outcome            TEXT NOT NULL,
    matched_by         TEXT NOT NULL DEFAULT '',
    candidate_ids      TEXT NOT NULL DEFAULT '[]',
    confidence         REAL,
    reason             TEXT NOT NULL DEFAULT '',
    resolver_version   TEXT NOT NULL,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_resolution_events_observation
    ON entity_resolution_events(observation_id);

CREATE TABLE IF NOT EXISTS entity_alignment_candidates (
    id                   INTEGER PRIMARY KEY,
    observed_name        TEXT NOT NULL,
    normalized_name      TEXT NOT NULL,
    entity_id            INTEGER NOT NULL REFERENCES entities(id),
    relation             TEXT NOT NULL DEFAULT 'suspected_same_entity'
                         CHECK(relation='suspected_same_entity'),
    status               TEXT NOT NULL DEFAULT 'suspected'
                         CHECK(status IN ('suspected','verified','rejected')),
    score                REAL NOT NULL DEFAULT 0.0,
    evidence_count       INTEGER NOT NULL DEFAULT 0,
    independent_sources  INTEGER NOT NULL DEFAULT 0,
    policy_version       TEXT NOT NULL,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    UNIQUE(normalized_name, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_alignment_candidates_queue
    ON entity_alignment_candidates(status, score DESC);

CREATE TABLE IF NOT EXISTS entity_alignment_evidence (
    id                 INTEGER PRIMARY KEY,
    candidate_id       INTEGER NOT NULL REFERENCES entity_alignment_candidates(id),
    source_snapshot_id INTEGER REFERENCES source_snapshots(id),
    observation_id     INTEGER REFERENCES observations(id),
    confidence         REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    reason             TEXT NOT NULL DEFAULT '',
    resolver_version   TEXT NOT NULL,
    created_at         REAL NOT NULL,
    UNIQUE(candidate_id, source_snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_alignment_evidence_candidate
    ON entity_alignment_evidence(candidate_id);

CREATE TABLE IF NOT EXISTS model_queue_reviews (
    id                 INTEGER PRIMARY KEY,
    queue_type         TEXT NOT NULL
                       CHECK(queue_type IN ('entity_alignment','type_conflict')),
    item_id            INTEGER NOT NULL,
    model              TEXT NOT NULL,
    verdict            TEXT NOT NULL,
    confidence         REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    reason             TEXT NOT NULL DEFAULT '',
    payload            TEXT NOT NULL DEFAULT '{}',
    policy_version     TEXT NOT NULL,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_queue_reviews_item
    ON model_queue_reviews(queue_type, item_id, created_at DESC);

CREATE TABLE IF NOT EXISTS entity_external_ids (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id),
    provider    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE(provider, external_id)
);

CREATE TABLE IF NOT EXISTS relation_definitions (
    name            TEXT PRIMARY KEY,
    registry_version INTEGER NOT NULL,
    family          TEXT NOT NULL,
    definition      TEXT NOT NULL,
    policy          TEXT NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id              INTEGER PRIMARY KEY,
    subject_id      INTEGER NOT NULL REFERENCES entities(id),
    relation        TEXT NOT NULL REFERENCES relation_definitions(name),
    object_id       INTEGER NOT NULL REFERENCES entities(id),
    qualifiers      TEXT NOT NULL DEFAULT '{}',
    qualifiers_hash TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK(status IN ('proposed','published','rejected','needs_evidence')),
    confidence      REAL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    CHECK(subject_id != object_id),
    UNIQUE(subject_id, relation, object_id, qualifiers_hash)
);
CREATE INDEX IF NOT EXISTS idx_claims_subject
    ON claims(subject_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_object
    ON claims(object_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_relation
    ON claims(relation, status);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    run_type        TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    model           TEXT NOT NULL DEFAULT '',
    prompt_version  TEXT NOT NULL DEFAULT '',
    config          TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running','completed','failed','cancelled')),
    started_at      REAL NOT NULL,
    finished_at     REAL
);

CREATE TABLE IF NOT EXISTS observations (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL REFERENCES runs(id),
    source_snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id),
    subject_text       TEXT NOT NULL DEFAULT '',
    subject_type       TEXT NOT NULL DEFAULT '',
    relation           TEXT NOT NULL DEFAULT '',
    object_text        TEXT NOT NULL DEFAULT '',
    object_type        TEXT NOT NULL DEFAULT '',
    excerpt            TEXT NOT NULL,
    location           TEXT NOT NULL DEFAULT '',
    payload            TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','resolved','rejected')),
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_status
    ON observations(status);

CREATE TABLE IF NOT EXISTS evidence (
    id                 INTEGER PRIMARY KEY,
    target_key         TEXT NOT NULL,
    entity_id          INTEGER REFERENCES entities(id),
    claim_id           INTEGER REFERENCES claims(id),
    source_snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id),
    polarity           TEXT NOT NULL
                       CHECK(polarity IN ('support','oppose','uncertain')),
    evidence_type      TEXT NOT NULL,
    excerpt            TEXT NOT NULL,
    excerpt_hash       TEXT NOT NULL,
    location           TEXT NOT NULL DEFAULT '',
    mechanically_valid INTEGER NOT NULL DEFAULT 0 CHECK(mechanically_valid IN (0,1)),
    entailment         TEXT NOT NULL DEFAULT 'unreviewed'
                       CHECK(entailment IN ('unreviewed','supports','contradicts','insufficient')),
    extraction_run_id  INTEGER REFERENCES runs(id),
    current_entailment_review_id INTEGER REFERENCES entailment_reviews(id),
    metadata           TEXT NOT NULL DEFAULT '{}',
    created_at         REAL NOT NULL,
    CHECK((entity_id IS NOT NULL AND claim_id IS NULL)
       OR (entity_id IS NULL AND claim_id IS NOT NULL)),
    UNIQUE(target_key, source_snapshot_id, excerpt_hash, polarity)
);
CREATE INDEX IF NOT EXISTS idx_evidence_claim
    ON evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_evidence_entity
    ON evidence(entity_id);

-- 蕴含判定的历史审核记录。只追加，不覆盖：evidence.entailment 只是当前结果的
-- 缓存，真正可复现的依据是这里的某一行。run_id 为空表示该判定早于留痕机制，
-- 来源不可考，reshadow --only-stale 会把它当作过期项重判。
CREATE TABLE IF NOT EXISTS entailment_reviews (
    id           INTEGER PRIMARY KEY,
    evidence_id  INTEGER NOT NULL REFERENCES evidence(id),
    run_id       INTEGER REFERENCES runs(id),
    verdict      TEXT NOT NULL
                 CHECK(verdict IN ('supports','contradicts','insufficient')),
    reason       TEXT NOT NULL DEFAULT '',
    raw_output   TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entailment_reviews_evidence
    ON entailment_reviews(evidence_id, id);

CREATE TABLE IF NOT EXISTS decisions (
    id                INTEGER PRIMARY KEY,
    target_key        TEXT NOT NULL,
    target_type       TEXT NOT NULL CHECK(target_type IN ('entity','claim','merge')),
    target_id         INTEGER NOT NULL,
    outcome           TEXT NOT NULL,
    decided_by        TEXT NOT NULL CHECK(decided_by IN ('human','auto','shadow')),
    policy_version    TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL DEFAULT '',
    evidence_snapshot TEXT NOT NULL DEFAULT '[]',
    -- [{"evidence_id":E,"entailment_review_id":R}]，裁决当时每条证据用的是哪次
    -- 蕴含判定。只存 evidence_id 不够：verdict 被重判后旧裁决就无法复现。
    evidence_review_snapshot TEXT NOT NULL DEFAULT '[]',
    batch_id          TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_target
    ON decisions(target_key, created_at);

CREATE TABLE IF NOT EXISTS merge_events (
    id                INTEGER PRIMARY KEY,
    source_entity_id  INTEGER NOT NULL REFERENCES entities(id),
    target_entity_id  INTEGER NOT NULL REFERENCES entities(id),
    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK(status IN ('proposed','applied','reverted','rejected')),
    decision_id       INTEGER REFERENCES decisions(id),
    reason            TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    CHECK(source_entity_id != target_entity_id)
);

-- 定向补证探查过的（Claim, 段落）。模型说「这段没有陈述该关系」也是结论，
-- 记下来避免下一轮重复问同一段。content_hash 变了才值得重问。
CREATE TABLE IF NOT EXISTS targeting_probes (
    id           INTEGER PRIMARY KEY,
    claim_id     INTEGER NOT NULL REFERENCES claims(id),
    ref          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    run_id       INTEGER REFERENCES runs(id),
    result       TEXT NOT NULL CHECK(result IN ('evidence','no_relation','failed')),
    reason       TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    UNIQUE(claim_id, ref, content_hash)
);

CREATE TABLE IF NOT EXISTS coverage_topics (
    id             TEXT PRIMARY KEY,
    parent_id      TEXT REFERENCES coverage_topics(id),
    name           TEXT NOT NULL,
    importance     REAL NOT NULL DEFAULT 1.0,
    policy         TEXT NOT NULL DEFAULT '{}',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reading_tasks (
    id                INTEGER PRIMARY KEY,
    coverage_topic_id TEXT NOT NULL REFERENCES coverage_topics(id),
    query             TEXT NOT NULL,
    source_hint       TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL,
    priority          REAL NOT NULL DEFAULT 0.0,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK(status IN ('pending','running','completed','failed','cancelled')),
    parent_task_id    INTEGER REFERENCES reading_tasks(id),
    source_derived    INTEGER NOT NULL DEFAULT 1 CHECK(source_derived IN (0,1)),
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reading_tasks_queue
    ON reading_tasks(status, priority DESC);

CREATE TABLE IF NOT EXISTS legacy_entity_map (
    legacy_node_id INTEGER PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entities(id),
    migrated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_claim_map (
    legacy_edge_id INTEGER PRIMARY KEY,
    claim_id       INTEGER NOT NULL REFERENCES claims(id),
    migrated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_issues (
    id              INTEGER PRIMARY KEY,
    legacy_item_type TEXT NOT NULL CHECK(legacy_item_type IN ('node','edge')),
    legacy_item_id  INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','resolved','ignored')),
    created_at      REAL NOT NULL,
    resolved_at     REAL,
    UNIQUE(legacy_item_type, legacy_item_id)
);

CREATE TABLE IF NOT EXISTS pipeline_processed (
    source_snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id),
    coverage_topic_id  TEXT NOT NULL REFERENCES coverage_topics(id),
    algorithm_version  TEXT NOT NULL,
    run_id             INTEGER NOT NULL REFERENCES runs(id),
    processed_at       REAL NOT NULL,
    PRIMARY KEY(source_snapshot_id, coverage_topic_id, algorithm_version)
);

INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
VALUES (1, 'claim_evidence_core', unixepoch());

INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
VALUES (2, 'legacy_migration_maps', unixepoch());

INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
VALUES (3, 'migration_issues', unixepoch());

INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
VALUES (4, 'pipeline_processed', unixepoch());

INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
VALUES (7, 'model_queue_reviews', unixepoch());
