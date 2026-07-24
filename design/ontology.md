# AI Knowledge Graph Ontology

## Purpose

This ontology is education-first while retaining first-class entities for the
methods, tasks, models, datasets, metrics, and systems needed to describe the AI
field accurately. The machine-readable authority is
`config/relation-registry.yaml`.

## Entity Types

- `field`: recognized research or teaching area.
- `concept`: abstract idea, property, mathematical object, or phenomenon.
- `method`: reusable algorithm, procedure, training strategy, or technique.
- `task`: problem definition with inputs, outputs, or a success criterion.
- `model`: named model family or concrete learned or specified model.
- `architecture`: reusable structural design for models or systems.
- `dataset`: named data collection used for training or evaluation.
- `metric`: defined evaluation measurement.
- `loss`: optimization objective or loss function.
- `system`: implemented framework, pipeline, training, or serving system.
- `resource`: paper, textbook, course, specification, or documentation.

## Granularity

An item is a first-class entity when an authoritative source discusses it
independently, it participates in a typed relation, it is a meaningful teaching
destination, or it needs independent provenance and history.

An item is a facet only when it is a local descriptive aspect that does not need
its own evidence or relations. A facet must be promoted when it acquires
independent sources, aliases, relations, conflicts, or learner evidence.
Promotion and demotion are reversible decisions, not destructive merges.

## Relation Semantics

### Taxonomy

- `is_a`: subject is a more specific kind of object.
- `subfield_of`: subject field is conventionally organized under object field.

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

- `alternative_to`: subject and object are alternative methods, models, or
  systems for substantially the same task or objective.
- `used_for`: subject supports or performs object task.
- `solves`: subject is explicitly presented as solving object task.
- `evaluated_by`: subject is evaluated using object metric.
- `trained_on`: subject model is trained or fine-tuned on object dataset.
- `optimizes`: subject method optimizes object loss or objective.

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
