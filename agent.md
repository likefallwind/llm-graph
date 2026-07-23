# LLM Graph Agent Guide

## Project Goal

Build a high-quality knowledge graph that systematically covers the full AI
field. The graph is primarily intended to support education, knowledge
navigation, and future personalized teaching.

Success is not measured by node or edge count alone. The graph must be:

- broad enough to cover all major AI subfields;
- semantically consistent across subfields;
- grounded in traceable source evidence;
- measurable through fixed quality and coverage benchmarks;
- safe to expand without silently degrading existing knowledge.

## Current Stage

The repository is a working MVP, not yet a full-domain knowledge graph system.
Its current strengths are source-grounded extraction, proposed/approved states,
human review, structural guards, and rollbackable automatic decisions.

The main architectural limitation is that a node or edge currently stores one
source and one rationale. This does not yet support multiple independent pieces
of evidence for the same knowledge claim.

## Highest-Priority Direction

Before substantially expanding the graph, design the next algorithm around:

1. A domain coverage map for the complete AI field.
2. A `concept -> claim -> evidence -> decision` data model.
3. Relation-specific validation instead of generic link-based support.
4. A fixed human-reviewed benchmark for measuring quality changes.
5. Coverage, recall, consistency, and provenance metrics in addition to
   approval precision.

Do not treat review-queue cleanup, prompt tuning, or adding more sources as a
substitute for this foundation.

## Core Principles

### Evidence First

- LLMs may extract, classify, and propose knowledge, but they are not the final
  knowledge source.
- Every approved claim must be traceable to one or more source snapshots.
- Preserve all supporting and contradicting evidence instead of overwriting or
  ignoring later evidence.
- Record the extractor model, prompt version, source version, and decision
  history needed to reproduce a claim.

### Separate Claims from Evidence

- A graph edge represents a canonical claim.
- A source excerpt represents evidence for or against that claim.
- Multiple sources must be able to support the same claim.
- Conflicting evidence must remain inspectable.
- Confidence must be derived from evidence and review results, not only from an
  LLM-provided score.

### Relation-Specific Validation

Do not use generic co-occurrence or hyperlinks as proof of a typed relation.

- `is_a` requires taxonomic evidence.
- `part_of` requires compositional evidence.
- `prerequisite_of` requires educational dependency evidence.
- Comparison, alternative, and historical-influence relations require explicit
  evidence matching their own semantics.
- Wikipedia category membership must not be automatically interpreted as
  `part_of`.

### Coverage Is Planned, Not Emergent

- Maintain an explicit hierarchy of AI subfields and core topics.
- Measure coverage per subfield and depth level.
- Expansion priority should be driven by documented coverage gaps, not only by
  sparse nodes, link popularity, or LLM suggestions.
- Keep classic foundations, current research areas, engineering systems,
  evaluation, safety, and applications visibly balanced.

### Granularity Must Be Consistent

- Define when an item is a standalone concept, a facet, a method, a task, a
  model, a metric, a dataset, or a resource.
- Do not rely only on an embedding threshold and one LLM judgment to decide
  granularity.
- Facets that require provenance, review, relationships, or learner evidence
  should become first-class records rather than untracked strings.

## Quality Gates

Before enabling broader automatic approval:

- build a fixed, versioned human gold set;
- report precision and recall by relation type and AI subfield;
- measure entity-resolution and granularity accuracy;
- measure unsupported-claim and orphan rates;
- audit automatic approvals with statistically meaningful samples;
- require evidence from genuinely independent sources where appropriate;
- run automatic decisions in shadow mode until the target threshold is met.

Current graph checks such as cycle detection, transitive redundancy, orphan
detection, and facet-shadow detection remain necessary, but they are not
sufficient evidence of semantic quality.

## Working Rules for Agents

- Inspect current code, database state, and evaluation output before making
  algorithm claims.
- Distinguish a local data-quality problem from an architectural algorithm
  problem.
- Prioritize systematic failure modes over individual bad nodes or edges.
- Do not expand the graph merely to demonstrate activity.
- Do not change relation semantics without updating the schema, extraction,
  validation, review, export, documentation, and benchmark together.
- Keep changes narrow and evidence-backed.
- Do not modify data or run automatic approval unless the user explicitly asks.
- When proposing the next step, explain how it improves full-domain coverage or
  measurable quality.

## Near-Term Definition of Done

The next algorithm foundation is complete when the repository can:

1. Store one canonical claim with multiple source-specific evidence records.
2. Retain supporting, opposing, and superseded evidence.
3. Evaluate each relation with relation-specific signals.
4. Report quality against a fixed human-reviewed benchmark.
5. Report coverage across an explicit AI domain taxonomy.
6. Re-run extraction or verification without losing provenance or silently
   discarding new evidence.
