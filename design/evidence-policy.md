# Evidence and Automatic Decision Policy

## Core Rule

An LLM output is an observation, not knowledge. Every published entity and claim
must be grounded in a versioned source snapshot and mechanically locatable
evidence.

```text
observation -> canonical claim -> evidence set -> decision
```

## Evidence Record

Each evidence item stores:

- claim identifier;
- source and immutable snapshot;
- exact excerpt and source location;
- `support`, `oppose`, or `uncertain` polarity;
- evidence type and original language;
- translation provenance when applicable;
- extraction run, model, and prompt version;
- mechanical validation and entailment results.

Mechanical validity proves provenance, not semantic correctness.

## Evidence Strength

Strong evidence includes explicit definitions, taxonomy, composition, function,
educational dependency, comparison, derivation, and relation-compatible
structured claims from authoritative sources.

Weak evidence includes table-of-contents order, hyperlinks, co-occurrence,
category membership, citations without explicit relations, and neighborhood
overlap. Weak evidence may prioritize reading or review, but cannot independently
approve a typed claim unless its relation policy explicitly permits it.

## Relation-Specific Authority

Authority is not one global high/mid/low label:

- textbooks can be strong for definitions and prerequisites;
- original papers can be strong for derivation and model details;
- benchmark documentation can be strong for datasets and metrics;
- Wikipedia can support terminology but is weak for prerequisites;
- Wikipedia categories are not `part_of` evidence.

Each source records authority by relation, freshness, original/derived status,
source family, and independence group.

## Independence

These are not automatically independent:

- a source and its translation;
- mirrors or copied summaries;
- multiple excerpts from one chapter;
- structured data derived from the same upstream page;
- repeated LLM judgments over the same evidence.

Independent support requires genuinely distinct source material or editorial
processes.

## Derived Claims

A derived claim is allowed only when every premise has evidence, the derivation
rule is registered and versioned, the reasoning chain is stored, the relation
permits inference, and a stricter approval threshold is used. Derived text may
not be presented as a quotation.

## LLM Roles

LLMs may act as grounded extractor, entity linker, relation classifier,
entailment judge, adversarial critic, and reading planner. Multiple calls to one
model are useful checks but are not independent knowledge sources.

## Decisions

Possible outcomes:

- `auto_approve`;
- `auto_reject`;
- `needs_more_evidence`;
- `human_review`.

Approval requires valid evidence, registered types and relation, valid
domain/range, no hard graph violation, calibrated entity resolution,
relation-specific evidence, positive entailment, no unresolved critical
objection, and sufficient benchmark data for the policy bucket.

Automatic rejection is limited to mechanical invalidity, schema invalidity,
explicit authoritative contradiction under the same qualifiers, or a calibrated
negative policy. Missing evidence produces `needs_more_evidence`.

Human review focuses on ambiguous merges, conflicting authority, ontology
changes, high-impact taxonomy and prerequisites, weak policy buckets, and
statistical audits.

## Activation Gate

Every automatic policy begins in shadow mode. The default target is at least
98% automatic approval precision per enabled policy bucket. High-impact merges,
taxonomy, and prerequisites may require stricter thresholds. No policy is
enabled from a tiny audit sample.
