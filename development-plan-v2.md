# LLM Graph v2 Development Plan

## 1. Objective

Build a high-quality, corpus-grounded knowledge graph that systematically covers
the AI field while minimizing routine human review.

LLMs may plan reading, extract observations, resolve entities, classify
relations, verify evidence, challenge conclusions, and prioritize review.
However, an LLM response is never itself accepted as knowledge. Every published
entity and claim must be grounded in a versioned source snapshot and mechanically
locatable evidence.

The v2 system should eventually support this loop:

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

Keep the existing repository and reuse its useful infrastructure, but replace
the graph representation, verification, and evaluation core.

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

### Isolation

- Keep v1 operational and read-only during initial v2 development.
- Create a separate `data/kg-v2.db`.
- Build v2 under `kg/v2/`.
- Do not modify or migrate the current database in place.
- Import v1 data only after the v2 vertical slice passes its quality gates.

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

Initial core tables:

```text
sources
source_snapshots
source_independence_groups

entities
aliases
entity_external_ids

claims
evidence
observations

extraction_runs
verification_runs
decisions
merge_events

relation_definitions
coverage_topics
coverage_expectations
reading_tasks

gold_examples
evaluation_runs
```

### 4.2 Entity Types

Initial entity types:

```text
concept
method
task
model
architecture
dataset
metric
loss
resource
```

Entity types are not cosmetic. They constrain which relations may connect two
entities.

### 4.3 Initial Relation Registry

Taxonomy and structure:

```text
is_a
subfield_of
part_of
```

Educational:

```text
prerequisite_of
often_confused_with
pedagogical_contrast_with
```

Functional and historical:

```text
used_for
solves
evaluated_by
trained_on
optimizes
derived_from
```

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

Decision classes:

```text
same_entity
different_entity
facet_of
ambiguous
```

Automatic merge requires a high-precision rule, such as a shared reliable
external identifier or a calibrated combination that meets the merge quality
gate. Ambiguous cases remain separate and enter targeted review.

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

`subfield_of` validation:

- recognized domain hierarchy;
- curriculum or classification support;
- distinction from `is_a` and `part_of`.

`part_of` validation:

- explicit composition evidence;
- rejection of category-membership-only evidence;
- distinction from taxonomy and topical membership.

`prerequisite_of` validation:

- explicit learning dependency;
- agreement across textbooks or course sequences;
- definition dependency;
- direction checks;
- cycle and shortcut checks.

Textbook order alone is weak evidence and cannot independently approve a
prerequisite claim.

Functional relations:

- enforce subject and object types;
- require text that entails the specific functional relationship;
- reject generic co-occurrence.

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

Create a versioned benchmark before enabling v2 automatic decisions.

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

## Phase 0: Freeze and Baseline

Tasks:

- freeze v1 graph expansion and real automatic approval;
- back up the current database;
- export current entities, edges, sources, evidence text, signals, and decisions;
- record current quality and coverage metrics;
- document known v1 failure examples.

Deliverables:

- `data/baseline/` export package;
- `reports/v1-baseline.json`;
- versioned failure-case set.

Exit criteria:

- v1 can be reproduced and compared with v2;
- no v1 data is required to be mutated during v2 development.

## Phase 1: Ontology, Evidence Policy, and Benchmark

Tasks:

- define entity types;
- define the initial relation registry;
- define source independence;
- define evidence acceptance rules;
- build the AI coverage taxonomy;
- produce the first 300 gold examples.

Deliverables:

- `design/ontology-v2.md`;
- `design/evidence-policy-v2.md`;
- `config/relation-registry-v1.yaml`;
- `config/ai-coverage-taxonomy-v1.yaml`;
- `benchmarks/gold-v1.jsonl`.

Exit criteria:

- every relation has unambiguous semantics and validation rules;
- benchmark examples exercise every relation and major failure mode.

## Phase 2: v2 Storage Foundation

Tasks:

- create `kg/v2/schema.sql`;
- implement source and snapshot storage;
- implement entities, aliases, and external identifiers;
- implement claims and evidence;
- implement immutable run records and reversible decisions;
- implement storage-level constraints and migrations for v2 only.

Proposed modules:

```text
kg/v2/models.py
kg/v2/store.py
kg/v2/sources.py
kg/v2/ontology.py
kg/v2/runs.py
```

Exit criteria:

- one claim can retain multiple supporting and opposing evidence records;
- later evidence is never silently discarded;
- every decision is reproducible and reversible.

## Phase 3: First Vertical Slice

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

Proposed modules:

```text
kg/v2/observations.py
kg/v2/extract.py
kg/v2/entity_resolution.py
kg/v2/claims.py
kg/v2/validators/
kg/v2/decision.py
kg/v2/pipeline.py
```

Exit criteria:

- the bounded domain runs end to end without v1 edge persistence;
- every output claim has inspectable evidence;
- results can be evaluated against the gold set;
- no real automatic decisions are required.

## Phase 4: Grounded Reading Agent

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

## Phase 5: Automated Verification and Active Review

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

## Phase 6: Coverage Planner

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

## Phase 7: v1 Import and Comparative Evaluation

Tasks:

- import v1 nodes as legacy entity observations;
- import v1 edges as unverified legacy claims;
- convert v1 source and rationale fields into legacy evidence;
- re-run entity resolution and relation validation;
- compare v1 and v2 on the same benchmark;
- retain rejected and changed claims for audit.

Exit criteria:

- no v1 item is silently promoted into the v2 published graph;
- every migrated claim receives v2 provenance and validation state;
- v2 outperforms v1 on agreed quality metrics.

## Phase 8: Cutover and Cleanup

Tasks:

- switch visualization and exports to v2;
- switch routine evolution commands to the v2 pipeline;
- archive the v1 database and modules;
- remove replaced v1 code only after cutover;
- publish operational and recovery documentation.

Exit criteria:

- v2 is the only writable knowledge core;
- the old system remains reproducible from its archive;
- rollback and recovery procedures are documented.

## 8. First Development Iteration

The first implementation iteration should not attempt full AI coverage.

Produce:

1. `design/ontology-v2.md`
2. `design/evidence-policy-v2.md`
3. `config/relation-registry-v1.yaml`
4. `config/ai-coverage-taxonomy-v1.yaml`
5. `benchmarks/gold-v1.jsonl`
6. `kg/v2/schema.sql`

Then implement one vertical slice:

```text
coverage task
  -> two source snapshots
  -> grounded observations
  -> entity resolution
  -> canonical claims
  -> multiple evidence records
  -> relation-specific validation
  -> shadow decision report
```

Do not implement broad automatic expansion before this slice passes the first
benchmark.

## 9. Decision Points Requiring User Approval

Confirm before implementation:

1. Whether the graph remains education-first or also models papers, products,
   organizations, and rapidly changing research artifacts.
2. The initial entity types and relation registry.
3. The first vertical-slice domain.
4. The quality threshold required before activating automatic approval.
5. Whether different LLM models should be used for extraction and verification,
   or one model should be used with source diversity as the main independence
   mechanism.

Recommended defaults:

- education-first, while modeling methods, tasks, models, datasets, and metrics;
- supervised-learning foundations as the first vertical slice;
- one primary LLM initially, with strict corpus grounding and independent source
  evidence;
- shadow mode until automatic approval precision is at least 98% in each
  enabled policy bucket.
