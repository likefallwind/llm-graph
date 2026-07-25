# LLM Graph Development Plan

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

## 2. Development Strategy

Develop directly on the `develop` branch. Reuse the useful infrastructure in
the repository while replacing the graph representation, verification, and
evaluation core in place. The stable `main` branch is the rollback boundary;
do not maintain parallel v1/v2 packages or databases.

### Reuse

- Wikipedia and document acquisition
- source YAML configuration
- source snapshots and content hashes
- HTML and PDF text extraction
- LLM request, concurrency, retry, and JSON parsing
- text chunking and mechanical evidence checks
- CLI conventions
- graph visualization concepts
- cycle, orphan, and redundancy guard ideas

### Replace

- single-source node and edge storage
- fixed one-row-per-edge evidence representation
- generic structural support for all relation types
- current automatic adjudication rules
- embedding-threshold-driven granularity decisions
- review-history-only calibration
- sparse-node and link-popularity-only expansion

### Branch Workflow

- All redesign work happens on `develop`.
- `main` remains the stable baseline until the redesigned pipeline passes.
- Refactor existing modules instead of creating versioned packages.
- Schema changes require a database backup and reversible migrations.
- Existing data is legacy input and must be revalidated before publication.

## 3. Non-Negotiable Invariants

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

## 4. Target Architecture

### 4.1 Storage

Current core tables:

```text
sources
source_snapshots

entities
aliases
entity_type_assertions
entity_resolution_events
entity_alignment_candidates
entity_alignment_evidence
entity_external_ids

claims
evidence
observations

runs
decisions
merge_events
model_queue_reviews

relation_definitions
coverage_topics
reading_tasks

legacy_entity_map
legacy_claim_map
migration_issues
pipeline_processed
```

Source independence is represented by the `sources.independence_group` field,
not by counting excerpts or repeated model judgments. Extraction and
verification share the versioned `runs` table.

### 4.2 Entity Types

Current entity types:

```text
field
concept
method
task
model
architecture
dataset
metric
loss
system
resource
```

Entity types are not cosmetic. They constrain which relations may connect two
entities.

### 4.3 Relation Registry v3

The first production-oriented graph intentionally exposes only three core
relations:

```text
is_a
part_of
prerequisite_of
```

This small registry is deliberate. The first graph version is primarily meant
to recover knowledge structure and learning order. Relations that are difficult
to distinguish at scale should be merged into these core semantics where that
is valid, or omitted until a later version.

The following relations remain registered as `experimental` and are not
available to the grounded extraction path:

```text
subfield_of
often_confused_with
pedagogical_contrast_with
alternative_to
used_for
solves
evaluated_by
trained_on
optimizes
derived_from
```

Experimental registration preserves design options without allowing them to
inflate the current graph or blur the educational structure.

Each relation definition must specify:

- allowed subject entity types;
- allowed object entity types;
- direction;
- symmetry;
- transitivity;
- acyclicity;
- inverse relation if applicable;
- accepted evidence types;
- minimum automatic approval policy;
- contradiction rules;
- relation-specific validator version.

Do not add a relation until these properties are defined.

## 5. Core Algorithms

### 5.1 Coverage Planner

Maintain a source-grounded AI domain taxonomy separate from the knowledge graph.
The initial taxonomy should be assembled from authoritative curricula, textbook
tables of contents, course outlines, and recognized classification systems.

Each coverage topic tracks:

- importance;
- expected entity categories;
- available authoritative sources;
- current entity coverage;
- claim density;
- multi-source evidence coverage;
- orphan rate;
- unresolved conflict rate;
- automatic approval audit quality;
- freshness.

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

This formula is a policy interface, not a permanent fixed equation. Its inputs
must be inspectable and benchmarked.

### 5.2 Corpus-Grounded Reading Agent

The agent receives a coverage task and a set of source snapshots. It may only
extract information present in those snapshots.

Output schema:

```text
entity observations
claim observations
facets
misconceptions
source-derived next reading targets
```

Every observation includes:

- exact evidence excerpt;
- source snapshot and location;
- explicit or inferred status;
- proposed entity types;
- proposed relation;
- extraction model and prompt version.

Next reading targets may come from:

- section headings;
- explicit terminology;
- hyperlinks;
- citations;
- indexes;
- source-derived search queries.

LLM memory may help rank or rephrase reading targets, but those targets remain
retrieval tasks and are never accepted directly as graph knowledge.

### 5.3 Entity Resolution

Candidate generation:

```text
normalized name
aliases
language-aware matching
external identifiers
source mappings
embedding similarity
domain and type compatibility
neighborhood compatibility
```

Resolution outcomes:

```text
same_entity
type_conflict
suspected_same_entity
created
ambiguous
```

Resolution order:

```text
deterministically normalized name
  -> exact canonical-name match
  -> exact verified-alias match
  -> limited candidate retrieval
  -> grounded LLM classification
  -> deterministic safety gate or suspected-alignment queue
```

Each entity has one canonical normalized name and may have multiple sourced
aliases. Exact canonical and verified-alias matches are the fast path. Fuzzy
string similarity is candidate retrieval only and never proves identity.

High-confidence direct translations and strict name variants may be verified
when the LLM classification also passes deterministic string checks. Examples
include Chinese/English term pairs and category-word variants such as
`分类`/`分类问题` or `Softmax 函数`/`softmax运算`. Abbreviations, symbols,
semantic aliases, and composite names are not automatically merged from one
model judgment.

Other likely matches enter `entity_alignment_candidates` as
`suspected_same_entity`. Evidence accumulates by independent source group.
Repeated judgments by MiniMax M3 are audit evidence, not additional independent
knowledge sources. Model queue reviews are stored separately in
`model_queue_reviews`.

All merges are stored as reversible events. Aliases retain language, source, and
history.

### 5.4 Claim Normalization and Aggregation

Observations are normalized into canonical `(subject, relation, object,
qualifiers)` claims.

Equivalent observations attach evidence to the same claim. They must not create
duplicate edges or silently discard later sources.

Evidence records:

```text
support
oppose
uncertain
```

The system must distinguish:

- several excerpts from one source;
- several sources in one source family;
- genuinely independent sources.

Translations, mirrors, and derived structured databases do not automatically
count as independent sources.

### 5.5 Relation-Specific Validation

Generic hyperlinks and co-occurrence cannot prove typed relations.

`is_a` validation:

- explicit taxonomic language;
- entity type compatibility;
- structured taxonomy evidence;
- direction consistency;
- conflict with existing taxonomy.

`part_of` validation:

- explicit evidence that the subject is an actual structural component or an
  explicitly identified process stage;
- rejection of usage, dependency, participation, construction, input/output,
  attribute, subtype, category-membership, and topical-membership evidence;
- explicit composition confirmation from the entailment judge before evidence
  can count as supporting;
- human review for high-impact claims.

`prerequisite_of` validation:

- explicit learning dependency;
- agreement across textbooks or course sequences;
- definition dependency;
- direction checks;
- cycle and shortcut checks.

Textbook order alone is weak evidence and cannot independently approve a
prerequisite claim.

Experimental relations are not extracted in the current vertical slice. Before
any one becomes core, it must receive endpoint rules, evidence contracts,
confusion tests, validators, migration analysis, and benchmark coverage.

### 5.6 LLM Verification Roles

The LLM can perform separate grounded roles:

1. Extractor
2. Entity linker
3. Relation classifier
4. Evidence entailment judge
5. Adversarial critic
6. Reading planner

Multiple calls to the same model are useful checks but are not independent
knowledge sources. Source diversity and deterministic validation provide the
actual independent support.

### 5.7 Decision Engine

Decision features:

- number of independent supporting sources;
- number and strength of opposing sources;
- source authority for the specific relation;
- exact evidence validity;
- entailment result;
- adversarial critique result;
- entity-resolution confidence;
- relation domain and range validity;
- structural constraint results;
- historical calibrated precision for the same policy bucket.

Decision outcomes:

```text
auto_approve
auto_reject
needs_more_evidence
human_review
```

Automatic rejection is allowed for mechanical invalidity, explicit
contradiction, or a calibrated negative decision. Lack of evidence alone yields
`needs_more_evidence`.

All automatic policies start in shadow mode. A policy may become active only
after its lower confidence bound meets the quality target on gold or audited
examples.

### 5.8 Active Review

Human review priority:

```text
review_priority =
    uncertainty
    * graph_impact
    * coverage_importance
    * conflict_level
    * expected_future_reuse
```

Humans should focus on:

- conflicting authoritative evidence;
- ambiguous entity merges;
- root taxonomy and high-impact prerequisite claims;
- ontology changes;
- statistically selected audits.

Routine, well-supported claims should be handled automatically after
calibration.

### 5.9 Graph Consistency

Hard constraints:

- relation domain and range;
- forbidden self-edges;
- required acyclicity;
- symmetric relation normalization;
- duplicate canonical claims;
- invalid external identifiers.

Soft anomaly detection:

- taxonomy versus composition conflicts;
- suspicious multiple parents;
- excessive or insufficient hierarchy depth;
- prerequisite shortcuts;
- disconnected high-value entities;
- facet/entity duplication;
- contradictory definitions;
- evidence conflicts;
- subfield coverage imbalance.

Hard violations block publication. Soft anomalies create review tasks.

## 6. Evaluation Plan

### 6.1 Gold Benchmark

Create a versioned benchmark before enabling redesigned automatic decisions.

Initial target: at least 300 human-reviewed examples covering:

- major AI subfields;
- every initial relation;
- positive, negative, wrong-direction, and wrong-type claims;
- multilingual aliases;
- same-name different-entity cases;
- entity versus facet decisions;
- simple and high-impact graph locations.

Gold examples must store reviewer rationale and source evidence.

### 6.2 Metrics

Entity resolution:

- candidate recall;
- same-entity precision and recall;
- automatic merge precision;
- granularity accuracy.

Claims:

- relation precision, recall, and F1;
- direction accuracy;
- relation-type confusion matrix;
- evidence entailment accuracy;
- unsupported published claim rate.

Automation:

- automatic approval precision;
- automatic rejection precision;
- human-review rate;
- audit overturn rate;
- decisions per human review minute.

Coverage:

- subfield coverage;
- core-topic coverage;
- entity-type coverage;
- multi-source evidence rate;
- orphan rate;
- unresolved conflict rate;
- source diversity.

### 6.3 Initial Quality Gates

- Published claims with no evidence: `0`.
- Mechanically invalid evidence accepted: `0`.
- Automatic entity merge precision: target at least `99%`.
- Automatic claim approval precision: target at least `98%` per enabled policy
  bucket.
- High-impact taxonomy and prerequisite claims require stricter policy or human
  review until sufficient calibration data exists.
- No automatic policy is enabled based only on a tiny audit sample.

Targets can be revised through documented benchmark evidence, not convenience.

## 7. Implementation Phases

### Current Status Snapshot — 2026-07-25

The redesigned grounded pipeline is operational on `develop` for a bounded
vertical slice. This is not yet approval to run unbounded expansion or publish
claims automatically.

Current database state:

```text
sources: 3
source snapshots: 9
entities: 90
claims: 40
evidence records: 164
observations: 193
decisions: 64
reading tasks: 30
processed source/topic pairs: 11
```

Current resolution and review state:

```text
aliases: 86 verified, 7 proposed, 2 rejected
alignment candidates: 39 verified, 3 suspected
observations: 181 resolved, 4 pending, 8 rejected
type conflicts: 17, all with MiniMax M3 queue reviews
model queue reviews: 21
```

The four review queues were processed in this order:

1. proposed aliases;
2. suspected entity alignments;
3. pending Observation replay;
4. entity type conflicts.

MiniMax M3 reviewed all four queues. Its conclusions were stored as evidence,
not treated as independent sources. Safe direct aliases were promoted through
deterministic gates. Pending replay reduced the queue from eight to four and
restored grounded evidence for the `二分类 is_a 分类` and
`多类分类 is_a 分类` claims. No entity primary type was changed solely from
the model's recommendation.

The full test suite currently contains 42 passing tests. Mechanical graph checks
report no cycles, reverse duplicate typed edges, or invalid endpoint types.
Existing soft warnings include prerequisite shortcuts, orphan nodes, and
facet/entity duplication; these remain quality work rather than hard schema
failures.

### Phase 0: Baseline — Partial

Tasks:

- avoid broad graph expansion and real automatic approval during the redesign;
- back up the current database;
- export current entities, edges, sources, evidence text, signals, and decisions;
- record current quality and coverage metrics;
- document known failure examples.

Deliverables:

- `data/baseline/` export package;
- `reports/baseline.json`;
- versioned failure-case set.

Exit criteria:

- the current baseline can be reproduced and compared with the redesign;
- database changes have a reversible migration path.

### Phase 1: Ontology, Evidence Policy, and Benchmark — Partial

Tasks:

- define entity types;
- define the initial relation registry;
- define source independence;
- define evidence acceptance rules;
- build the AI coverage taxonomy;
- produce the first 300 gold examples.

Deliverables:

- `design/ontology.md`;
- `design/evidence-policy.md`;
- `config/relation-registry.yaml`;
- `config/ai-coverage-taxonomy.yaml`;
- `benchmarks/gold.jsonl`.

Exit criteria:

- every relation has unambiguous semantics and validation rules;
- benchmark examples exercise every relation and major failure mode.

Current note: ontology, evidence policy, relation registry v3, coverage taxonomy,
and the benchmark schema exist. The gold dataset is still only a seed and is far
below the 300-example quality gate.

### Phase 2: Storage Foundation — Implemented

Tasks:

- create `kg/schema.sql` and explicit migrations;
- implement source and snapshot storage;
- implement entities, aliases, and external identifiers;
- implement claims and evidence;
- implement immutable run records and reversible decisions;
- implement storage-level constraints and reversible migrations.

Implemented modules:

```text
kg/models.py
kg/store.py
kg/schema.py
kg/schema.sql
kg/ontology.py
```

Exit criteria:

- one claim can retain multiple supporting and opposing evidence records;
- later evidence is never silently discarded;
- every decision is reproducible and reversible.

### Phase 3: First Vertical Slice — Operational, Not Yet Calibrated

Scope:

- one bounded domain, recommended: supervised-learning foundations;
- three relations: `is_a`, `part_of`, and `prerequisite_of`;
- two or more independent textbook or curriculum sources plus Wikipedia as
  supplementary material.

Tasks:

- adapt existing corpus and document readers;
- implement observation extraction;
- implement mechanical evidence validation;
- implement entity resolution;
- implement claim aggregation;
- implement relation-specific validators;
- generate a shadow decision report.

Implemented modules:

```text
kg/observations.py
kg/entity_resolution.py
kg/claims.py
kg/validators.py
kg/decision.py
kg/pipeline.py
kg/review_queues.py
```

Exit criteria:

- the bounded domain runs end to end using redesigned claim persistence;
- every output claim has inspectable evidence;
- results can be evaluated against the gold set;
- no real automatic decisions are required.

Current note: the redesigned pipeline runs end to end for supervised-learning
foundations, uses the three core relations, preserves evidence separately from
claims, performs relation-specific entailment validation, and emits shadow
decisions. It still needs broader independent-source coverage and benchmark
evaluation before this phase is considered complete.

### Phase 4: Grounded Reading Agent — Foundation Implemented

Tasks:

- implement reading-task queues;
- extract next targets from headings, terms, links, citations, and indexes;
- enforce retrieval-before-knowledge;
- detect repeated reading loops;
- track source and topic coverage;
- add source-family and independence handling.

Exit criteria:

- the graph can expand through corpus reading without accepting LLM memory as
  knowledge;
- every expansion path is traceable from coverage task to source to claim.

Current note: document and Wikipedia readers, reading tasks, source snapshots,
source independence groups, per-chunk extraction limits, and processed-source
tracking are implemented. Repeated-loop control and coverage-driven scheduling
still need production evaluation.

### Phase 5: Automated Verification and Active Review — In Progress

Tasks:

- implement grounded entailment verification;
- implement adversarial critique;
- calibrate relation-specific policies;
- implement shadow decisions;
- implement active-review prioritization;
- implement stratified random audits.

Exit criteria:

- enabled automatic policy buckets meet their benchmark quality gates;
- conflicts and ambiguous merges are routed to humans;
- routine review workload falls without reducing measured precision.

Current note: grounded entailment, shadow decisions, entity-alignment queues,
model review audit records, pending Observation replay, and type-conflict
triage are implemented. Adversarial critique, calibrated active-review
prioritization, and stratified audits remain incomplete.

### Phase 6: Coverage Planner — Early Foundation

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

### Phase 7: Legacy Data Migration and Comparative Evaluation — Code Ready, Not Run

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

Current note: migration tables and preview/apply code exist, but the current
database has no migrated legacy entities or claims. Comparative evaluation has
not started.

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

## 8. Current Development Iteration

The next iteration should prepare a controlled expansion, not immediately run
unbounded full-graph growth.

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

The four pending observations depend on unresolved alignment for
`牛顿-拉弗森法` and `平方误差函数`; replay them after human decisions.

### 8.2 Add Independent Evidence

- read at least one genuinely independent authoritative source for the current
  core claims;
- prioritize `is_a` and `prerequisite_of` claims with only one source group;
- do not count translations, repeated chapters from the same book family, or
  repeated MiniMax M3 reviews as independent;
- use relation-specific authority: textbooks and curricula are usually stronger
  for learning order, while Wikipedia remains supplementary rather than
  globally inferior or superior.

### 8.3 Build the Calibration Set

- expand `benchmarks/gold.jsonl` from its seed state toward at least 300 reviewed
  examples;
- include multilingual aliases, abbreviations, semantic aliases, composite
  names, wrong directions, type conflicts, and hard `part_of` negatives;
- report precision by policy bucket instead of one aggregate number;
- keep every automatic policy in shadow mode until its lower confidence bound
  reaches the quality gate.

### 8.4 Run Controlled Expansion

After the human queue and first calibration batch are complete:

1. process a bounded batch of authoritative textbook sections;
2. process supplementary Wikipedia pages for terminology and coverage gaps;
3. run alias review, alignment review, pending replay, and type review after
   each batch;
4. run relation entailment validation and graph guards;
5. compare queue growth, orphan rate, evidence independence, and audit quality;
6. increase batch size only when review debt stays bounded.

The extraction limits are per text chunk, not global per document. They protect
model output quality without truncating the rest of a long chapter.

## 9. Decisions Now Fixed

The following choices are no longer open design questions:

1. The graph is education-first.
2. The first vertical slice is supervised-learning foundations.
3. The core relation set is `is_a`, `part_of`, and `prerequisite_of`.
4. Knowledge-structure and learning-order accuracy take priority over broad
   functional or analogy relations.
5. MiniMax M3 may extract, translate, normalize, and review, but a model judgment
   is never an independent source.
6. Exact canonical or verified-alias matches are the entity-resolution fast
   path; fuzzy matching only retrieves candidates.
7. Automatic publication remains in shadow mode until benchmarked policy
   buckets meet the quality threshold.

Future ontology expansion, activation of experimental relations, and automatic
main-type changes still require explicit review and migration analysis.
