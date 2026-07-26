# AI Knowledge Graph Ontology

## Purpose

This ontology is education-first while retaining first-class entities for the
solutions, tasks, data, and criteria needed to describe the AI field accurately.
The machine-readable authority is `config/relation-registry.yaml`.

## Entity Types

One dimension, six values, single-valued and required. They are decided by a
priority order, first match wins:

1. `resource`: something people read, study, or cite.
2. `criterion`: a standard, metric, objective, or protocol used to optimize,
   compare, score, or evaluate.
3. `data`: a collection of samples, records, instances, or observations.
4. `task`: a problem with a goal, inputs and outputs, or a success condition.
5. `solution`: an algorithm, process, model, architecture, system, or tool used
   to solve or support a task. The test is whether it has executable steps or
   structure.
6. `concept`: an abstract knowledge object that occupies none of the five slots
   above — a mathematical object, operation, property, law, phenomenon, or
   quantity.

The priority order exists for one reason: `concept` co-applies with every other
slot, since a loss function is also a concept and backpropagation is also a
concept. The order says that occupying a functional slot wins. It is not a
tie-break among the first five — those should never tie, and a tie means two
things are sharing one name.

The full criteria, with positive and negative examples for each type and five
cross-cutting disambiguation clauses, live in `config/relation-registry.yaml`
and reach the model through `Registry.entity_type_contract()`. That file is the
only copy; prompts generate from it rather than restating it.

The primary type names an entity's canonical category so that resolution,
conflict detection, and relation constraints have something deterministic to
work with. It does not claim to capture everything the entity is used for —
those other facets are the pattern of its neighbouring edges, which is why they
are not stored as node attributes.

Type is judged from the entity's definition, never from the spelling of its
name. An entity whose definition is too thin to judge is dropped rather than
guessed at, because the primary type is written once at creation and can only
be changed afterwards through `kg pipeline retype`.

Two deterministic checks fall out of a stable type system, both zero-LLM. `is_a`
must not cross types, since a subtype and its supertype occupy the same slot;
crossings are reported by `kg pipeline taxonomy-types`. And suffix-stripped name
variants (`回归问题` → `回归`) must not cross types either, since the stripped
suffixes are exactly the type markers.

The migration from the previous eleven-type vocabulary is recorded in
`design/entity-type-v5.md`.

## Granularity

An item is a first-class entity when an authoritative source discusses it
independently, it participates in a typed relation, it is a meaningful teaching
destination, or it needs independent provenance and history.

The new core has no facet type and no promotion or demotion mechanism. The
legacy core stored sub-aspects as facet nodes, including misconceptions under
the `误区:` name prefix; that representation is not carried forward. In the new
core there are only entities, so an item that does not clear the first-class bar
above is simply not stored, and one that does is an ordinary entity with its own
evidence, aliases, and provenance.

A misconception is therefore an entity in its own right, related to what it is
confused with by `often_confused_with`. That relation is currently
`experimental` in the registry and unavailable to the extraction path, so
misconceptions cannot yet be captured. Activating it requires the same evidence
and validator work as any other relation promotion.

Merging two entities that turn out to be the same thing is a separate
mechanism — `store.merge_entities`, reversible through `merge_events.payload`.
It is not facet promotion.

## Relation Semantics

### Taxonomy

- `is_a`: subject is a more specific kind of object. Both endpoints must have
  the same primary type.
- `subfield_of`: subject field is conventionally organized under object field.
  Retiring this is proposed in `design/entity-type-v5.md`: with `field` folded
  into `concept` its signature is `concept → concept`, indistinguishable from
  `is_a`, and `tests/test_relation_contract.py` already asserts that field
  taxonomy travels through `is_a`.

Taxonomy is not topical co-occurrence or composition.

### Composition

- `part_of`: subject is a component, stage, or structural part of object.

Wikipedia category membership is not composition evidence.

### Educational

- `prerequisite_of`: understanding subject is materially required for object at
  the stated scope.
- `often_confused_with`: teaching material documents a common confusion.
- `pedagogical_contrast_with`: teaching material explicitly compares two items.

Textbook order, historical order, and co-occurrence do not independently prove
a prerequisite.

### Functional

- `alternative_to`: two solutions are alternatives for substantially the same
  task or objective.
- `used_for`: subject supports or performs object task.
- `solves`: subject is explicitly presented as solving object task.
- `evaluated_by`: subject is evaluated using object criterion.
- `trained_on`: subject solution is trained or fine-tuned on object data.
- `optimizes`: subject solution optimizes object criterion or objective concept.

These are the relations the functional slots exist to constrain — `task` is what
`solves` and `used_for` point at, `data` is what `trained_on` points at,
`criterion` is what `evaluated_by` and `optimizes` point at. All six are still
`experimental` and therefore outside the extraction path, so those constraints
do not bite yet.

### Historical

- `derived_from`: subject is explicitly derived, extended, or adapted from
  object. Similarity and chronology alone are insufficient.

## Claim Qualifiers

Initial structured qualifiers:

- `scope`: course, task, textbook, or subfield context;
- `condition`: assumptions under which a claim holds;
- `variant`: named variant or configuration;
- `time`: relevant publication or version period;
- `inference`: `explicit` or `derived`;
- `strength`: required, recommended, weak, or contextual.

Materially different qualifiers create distinct claims.

## Change Policy

Adding or changing an entity type or relation requires:

1. a semantic definition;
2. subject and object type constraints;
3. direction, symmetry, transitivity, and acyclicity rules;
4. accepted and rejected evidence examples;
5. validator behavior;
6. gold benchmark examples;
7. migration impact analysis.

Extraction prompts may not introduce unregistered relations.
