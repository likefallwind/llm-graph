# LLM Graph Development Plan

What is still open. Implemented behavior is described in
`design/architecture.md`; this file keeps the objective, the invariants, the
designs not yet built, and the current iteration.

## 1. Objective

Build a high-quality, corpus-grounded knowledge graph that systematically covers
the AI field while minimizing routine human review.

LLMs may plan reading, extract observations, resolve entities, classify
relations, verify evidence, challenge conclusions, and prioritize review.
However, an LLM response is never itself accepted as knowledge. Every published
entity and claim must be grounded in a versioned source snapshot and mechanically
locatable evidence.

The redesigned system should eventually support this loop:

```text
AI coverage gap
  -> select authoritative sources
  -> read source snapshots
  -> extract grounded observations
  -> resolve entities
  -> aggregate canonical claims
  -> collect supporting and opposing evidence
  -> run relation-specific validation
  -> auto-decide or request targeted human review
  -> update coverage and quality metrics
  -> schedule the next reading task
```

The first four steps and the validation path exist. Coverage-driven selection
and scheduling do not.

## 2. Non-Negotiable Invariants

1. Every published entity has at least one source-backed description.
2. Every published claim has at least one mechanically locatable evidence item.
3. Claims and evidence are separate records.
4. Multiple sources can support or oppose the same claim.
5. Source independence is explicitly represented.
6. An LLM-generated search target cannot become an entity without source
   evidence.
7. Missing evidence means pending, not automatically false.
8. Entity merges and automatic decisions are reversible.
9. Relation semantics are enforced through domain, range, direction, symmetry,
   and validation policies.
10. Every algorithm, model, prompt, and source snapshot used in a decision is
    versioned.

Schema changes require a database backup and reversible migrations. Existing
legacy data is input, not knowledge, and must be revalidated before publication.

## 3. Relation Scope

The graph intentionally exposes only three core relations: `is_a`, `part_of`,
and `prerequisite_of`. The first version is meant to recover knowledge structure
and learning order. Relations that are hard to distinguish at scale should be
merged into these where valid, or omitted until later.

Entity types follow the same principle and were cut from eleven to six in
registry v5: `resource`, `criterion`, `data`, `task`, `solution`, `concept`.
The eleven-type vocabulary produced a 16.7% type-conflict rate because adjacent
categories overlapped and the prompt shipped bare type names with no criteria.
The six are one dimension decided by priority order, and the criteria — with
positive and negative examples — are generated from the registry into every
prompt that judges a type. `design/entity-type-v5.md` records the migration,
the four rounds of criteria iteration that produced it, and what is still open.

Ten relations stay registered as `experimental` and are not available to the
extraction path: `subfield_of`, `often_confused_with`,
`pedagogical_contrast_with`, `alternative_to`, `used_for`, `solves`,
`evaluated_by`, `trained_on`, `optimizes`, `derived_from`. Experimental
registration preserves design options without letting them inflate the graph.

Before any experimental relation becomes core it needs endpoint rules, evidence
contracts, confusion tests, a validator, migration analysis, and benchmark
coverage. Do not add a relation until every field required by
`kg/ontology.py` is defined.

## 4. Designs Not Yet Built

### 4.1 Coverage Planner

Maintain a source-grounded AI domain taxonomy separate from the knowledge graph,
assembled from authoritative curricula, textbook tables of contents, course
outlines, and recognized classification systems.

Each coverage topic should track importance, expected entity categories,
available authoritative sources, current entity coverage, claim density,
multi-source evidence coverage, orphan rate, unresolved conflict rate,
automatic approval audit quality, and freshness.

Reading-task priority:

```text
priority =
    domain_importance
    * coverage_gap
    * source_availability
    * expected_learning_value
    * quality_deficit
    / expected_review_cost
```

This is a policy interface, not a permanent equation. Its inputs must be
inspectable and benchmarked.

Current state: `coverage_topics` and `reading_tasks` exist and are written to.
Nothing reads `reading_tasks`. `pipeline.batch` picks sections by
`doc_sections.ord`, so expansion order is arbitrary with respect to coverage.

### 4.2 Adversarial Critique

The LLM currently fills five grounded roles: extractor, entity linker, relation
classifier, evidence entailment judge, reading planner. The sixth — adversarial
critic, asked to argue against a claim its own extractor produced — is not
implemented.

Multiple calls to the same model are useful checks but are not independent
knowledge sources.

### 4.3 Calibrated Decision Policy

`validators.evaluate` currently decides from independent source count, high
authority count, opposing evidence, entailment verdict, and structural
constraints. The features it does not use yet:

- entity-resolution confidence;
- adversarial critique result;
- historical calibrated precision for the same policy bucket.

`auto_reject` is defined but never produced. Automatic rejection should be
allowed for mechanical invalidity, explicit contradiction, or a calibrated
negative decision. Lack of evidence must stay `needs_more_evidence`.

A policy may leave shadow mode only after its lower confidence bound meets the
quality target on gold or audited examples.

### 4.4 Active Review

Human review priority:

```text
review_priority =
    uncertainty
    * graph_impact
    * coverage_importance
    * conflict_level
    * expected_future_reuse
```

Humans should focus on conflicting authoritative evidence, ambiguous entity
merges, root taxonomy and high-impact prerequisite claims, ontology changes, and
statistically selected audits. Routine well-supported claims should be handled
automatically after calibration.

Current state: the four review queues exist but are unordered within themselves.
Stratified random audits do not exist.

### 4.5 Soft Anomaly Detection

Hard constraints (domain/range, self-edges, acyclicity, symmetric
normalization, duplicate canonical claims) are enforced. Soft anomalies are not
detected:

- taxonomy versus composition conflicts;
- suspicious multiple parents;
- excessive or insufficient hierarchy depth;
- prerequisite shortcuts;
- disconnected high-value entities;
- contradictory definitions;
- evidence conflicts;
- subfield coverage imbalance.

Hard violations block publication. Soft anomalies should create review tasks.

## 5. Evaluation Plan

### 5.1 Gold Benchmark

A versioned benchmark must exist before any redesigned automatic decision is
enabled.

Target: at least 300 human-reviewed examples covering major AI subfields, every
core relation, positive/negative/wrong-direction/wrong-type claims, multilingual
aliases, same-name different-entity cases, and both simple and high-impact graph
locations. Gold examples store reviewer rationale and source evidence.

Current state: `benchmarks/gold.schema.json` is defined and `gold.jsonl` holds
3 reviewed negatives — all `part_of` claims the pipeline produced and a human
rejected. Provenance archived in `data/archive/bad-claims-20260726.json`.

### 5.2 Metrics

Entity resolution: candidate recall; same-entity precision and recall;
automatic merge precision; granularity accuracy.

Claims: relation precision, recall, and F1; direction accuracy; relation-type
confusion matrix; evidence entailment accuracy; unsupported published claim
rate.

Automation: automatic approval precision; automatic rejection precision;
human-review rate; audit overturn rate; decisions per human review minute.

Coverage: subfield coverage; core-topic coverage; entity-type coverage;
multi-source evidence rate; orphan rate; unresolved conflict rate; source
diversity.

None of these are computed yet.

### 5.3 Quality Gates

- Published claims with no evidence: `0`.
- Mechanically invalid evidence accepted: `0`.
- Automatic entity merge precision: at least `99%`.
- Automatic claim approval precision: at least `98%` per enabled policy bucket.
- High-impact taxonomy and prerequisite claims require stricter policy or human
  review until sufficient calibration data exists.
- No automatic policy is enabled based only on a tiny audit sample.

Targets can be revised through documented benchmark evidence, not convenience.

## 6. Status Snapshot — 2026-07-26

The pipeline is operational for a bounded vertical slice: supervised-learning
foundations, three core relations, two or more independent textbook sources plus
Wikipedia. This is not approval to run unbounded expansion or publish
automatically.

```text
sources: 7                  entities: 114
source snapshots: 19        claims: 44 (is_a 20, part_of 17, prerequisite_of 7)
evidence records: 213       observations: 262
entailment reviews: 89      decisions: 162
targeting probes: 31        reading tasks: 38
processed source/topic pairs: 12
```

```text
aliases: 115 verified, 29 proposed, 2 rejected
alignment candidates: 68 verified, 7 suspected
observations: 231 resolved, 9 pending, 22 rejected
type conflicts: 17, all with MiniMax M3 queue reviews
entity merges performed: 0
```

Shadow decision distribution under registry v4:

```text
needs_more_evidence: 32
human_review: 12
```

No claim is auto-approvable. `human_review` does not mean the evidence is
adequate — all three core relations carry `high_impact_review: true`, and some
of these claims also have opposing evidence.

`pipeline identity` reports 18 of 57 claim evidence records that do not mention
an identity name of one endpoint, 16 of which still count as strong evidence.
Fifteen of the eighteen come from the extraction path, not from targeting. This
is the largest open data-quality gap.

123 tests pass. Mechanical graph checks report no cycles, reverse duplicate
typed edges, or invalid endpoint types.

## 7. Remaining Phases

### Phase 5: Automated Verification and Active Review — In Progress

Built: grounded entailment verification with append-only review history, shadow
decisions, entity-alignment queues, model review audit records, pending
Observation replay, type-conflict triage, claim-directed retrieval.

Remaining: adversarial critique, calibrated active-review prioritization,
stratified audits.

Exit criteria:

- enabled automatic policy buckets meet their benchmark quality gates;
- conflicts and ambiguous merges are routed to humans;
- routine review workload falls without reducing measured precision.

### Phase 6: Coverage Planner — Not Started

Tasks:

- compute coverage metrics by AI subfield;
- generate reading tasks from coverage gaps;
- balance foundational, modern, engineering, evaluation, and safety topics;
- implement freshness and source-diversity priorities;
- produce a coverage dashboard.

Exit criteria:

- the system can explain why a topic is selected next;
- expansion is not dominated by link popularity or current graph proximity;
- coverage progress is measurable over time.

### Phase 7: Legacy Data Migration — Code Ready, Not Run

Tasks:

- import existing nodes as legacy entity observations;
- import existing edges as unverified legacy claims;
- convert existing source and rationale fields into legacy evidence;
- re-run entity resolution and relation validation;
- compare the baseline and redesign on the same benchmark;
- retain rejected and changed claims for audit.

Exit criteria:

- no legacy item is silently promoted into the published graph;
- every migrated claim receives complete provenance and validation state;
- the redesign outperforms the baseline on agreed quality metrics.

Blocker: `legacy_migration.RELATED_MAP` maps legacy `related_to` edges onto
three relations that are all `experimental`. Decide whether to promote them
properly or drop those edges before running the migration. Do not bypass
`active_only` for the migration's convenience.

### Phase 8: Cutover and Cleanup — Pending

Tasks:

- switch visualization and exports to the redesigned claim model;
- switch routine evolution commands to the redesigned pipeline;
- archive the pre-redesign database;
- remove replaced code only after cutover;
- publish operational and recovery documentation.

Exit criteria:

- the redesigned store is the only writable knowledge core;
- the old system remains reproducible from its archive;
- rollback and recovery procedures are documented.

The legacy core was frozen on 2026-07-26 and the new core no longer depends on
it — see `design/architecture.md` §2. What remains is switching the read-side
commands over and archiving.

## 8. Current Development Iteration

Prepare a controlled expansion, not unbounded growth.

### 8.1 Close the High-Impact Human Queue

Human confirmation should focus on the small set where model evidence is not
sufficient:

- alignment:
  - `牛顿-拉弗森法` versus `牛顿法`;
  - `平方误差函数` versus `平方损失`;
  - `softmax-交叉熵损失` versus `交叉熵损失`;
- type:
  - whether `单层神经网络` should change from `model` to `architecture`;
  - whether `线性代数` and `微积分` should change from `concept` to `field`;
  - whether `人工神经网络` is best represented as `method`, `model`, or
    `architecture`;
- remaining proposed abbreviations and semantic aliases, including `ce`,
  `minibatch SGD`, and `稠密层`.

Pending observations depend on unresolved alignment; replay them after human
decisions.

### 8.2 Close the Identity-Mention Gap

Fifteen evidence records from the extraction path do not mention an identity
name of one endpoint yet still count as strong evidence. The extraction prompt
is `kg/observations.py:13`. Decide whether to reject such evidence mechanically
or fix the prompt; measure with `pipeline identity` before and after.

### 8.3 Add Independent Evidence

- prioritize `is_a` and `prerequisite_of` claims with only one source group;
- prefer claim-directed retrieval over reading another book end to end;
- do not count translations, repeated chapters from the same book family, or
  repeated MiniMax M3 reviews as independent;
- use relation-specific authority: textbooks and curricula are usually stronger
  for learning order, while Wikipedia remains supplementary rather than globally
  inferior or superior.

### 8.4 Build the Calibration Set

- grow `benchmarks/gold.jsonl` toward at least 300 reviewed examples;
- include multilingual aliases, abbreviations, semantic aliases, composite
  names, wrong directions, type conflicts, and hard `part_of` negatives;
- report precision by policy bucket instead of one aggregate number;
- keep every automatic policy in shadow mode until its lower confidence bound
  reaches the quality gate.

### 8.5 Run Controlled Expansion

After the human queue and first calibration batch are complete:

1. process a bounded batch of authoritative textbook sections;
2. process supplementary Wikipedia pages for terminology and coverage gaps;
3. run alias review, alignment review, pending replay, and type review after
   each batch;
4. run relation entailment validation and graph guards;
5. compare queue growth, orphan rate, evidence independence, and audit quality;
6. increase batch size only when review debt stays bounded.

## 9. Decisions Now Fixed

No longer open design questions:

1. The graph is education-first.
2. The first vertical slice is supervised-learning foundations.
3. The core relation set is `is_a`, `part_of`, and `prerequisite_of`.
4. Knowledge-structure and learning-order accuracy take priority over broad
   functional or analogy relations.
5. MiniMax M3 may extract, translate, normalize, and review, but a model
   judgment is never an independent source.
6. Exact canonical or verified-alias matches are the entity-resolution fast
   path; fuzzy matching only retrieves candidates.
7. Automatic publication remains in shadow mode until benchmarked policy buckets
   meet the quality threshold.
8. `config/relation-registry.yaml` is the only place where relation semantics
   and evidence-type strength are defined. Code reads it; code does not restate
   it.
9. The legacy core is frozen and read-only. New work goes to the new core, and
   the isolation is enforced by tests rather than by convention.
10. There is no facet mechanism in the new core. Anything worth naming is an
    entity; anything not worth naming is not stored.

Future ontology expansion, activation of experimental relations, and automatic
main-type changes still require explicit review and migration analysis.
