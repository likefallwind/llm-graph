# Gold Benchmark

The benchmark measures algorithm quality. It must not contain facts generated
from LLM memory.

## Collection

1. Select examples across the AI coverage taxonomy.
2. Attach immutable source snapshots and exact excerpts.
3. Human-review entity identity, granularity, relation, direction, entailment,
   and final validity.
4. Record the review rationale.
5. Include positive examples and hard negatives.
6. Keep test labels out of extraction and verification prompts.

The first release targets at least 300 reviewed examples covering every relation,
major AI subfields, multilingual aliases, name ambiguity, facets, wrong types,
wrong directions, unsupported plausible claims, source conflicts, root taxonomy,
and high-impact prerequisites.

`gold.schema.json` defines each JSONL record. `gold.jsonl` remains empty until
source-backed annotation is performed. An empty benchmark is more honest than
an ungrounded benchmark.

Splits are `train`, `validation`, and `test`. Label corrections require a
documented review event rather than silent replacement.
