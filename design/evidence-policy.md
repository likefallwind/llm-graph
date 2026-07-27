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

The vocabulary and the strength of each type live in
`config/relation-registry.yaml` under `evidence_types`. This document explains
the intent; the registry is what the code enforces.

Strong types state a relation in the text itself: `explicit_definition`,
`explicit_taxonomy`, `explicit_composition`, `explicit_function`,
`explicit_prerequisite`, `explicit_comparison`, `explicit_derivation`.

Weak types are arrangement rather than assertion: `toc_order`, `hyperlink`,
`cooccurrence`. Category membership, citations without an explicit relation, and
neighborhood overlap fall here as well. Weak evidence may prioritize reading or
review, but never counts toward the approval threshold of a typed claim.

These two properties answer different questions and must not be collapsed.

Global strength answers **is this text an assertion at all**. Weak types are
arrangement, not assertion, so they can neither support nor oppose a claim. A
co-occurrence proves nothing in either direction.

`accepted_evidence_types` answers **can this kind of assertion establish this
relation**. It filters supporting evidence only. `explicit_function` is strong
for `used_for` and excluded from `part_of`, because "A is used for B" is exactly
the reading `part_of` rules out. A relation may accept only a subset of the
strong types; the ones it does not accept contribute nothing to the threshold —
they are not downgraded to weak, they are ignored.

Being unable to establish a relation does not make an assertion unable to
oppose it. A definition of `特征` as "the independent variables a prediction is
based on" cannot establish `特征 part_of 样本`, but it is a valid rebuttal of
it. Opposing evidence is filtered by global strength only.

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
