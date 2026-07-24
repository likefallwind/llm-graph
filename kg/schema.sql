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
    created_at        REAL NOT NULL,
    UNIQUE(entity_id, normalized_name, language)
);
CREATE INDEX IF NOT EXISTS idx_aliases_normalized
    ON aliases(normalized_name);

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

INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
VALUES (1, 'claim_evidence_core', unixepoch());
