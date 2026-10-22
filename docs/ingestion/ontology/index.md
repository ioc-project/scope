# The SCOPE ontology

<p class="eyebrow">The schema the knowledge graph follows</p>

The **MAS execution ontology** is the schema of SCOPE's knowledge graph. Normalisation
expresses a session's OpenTelemetry spans in its terms, and every later worker adds a layer
on top of it. It ships as **five Turtle ontologies**, which share one namespace, together with
SHACL shapes and the `scope-ontology` Python package.

```turtle
@prefix mas: <https://ioc-project.github.io/scope-ontology/mas#> .
```

## The five ontologies

Each ontology is a layer of the graph, written by one stage of the pipeline. Its page gives a
short description and links to the Widoco reference documentation.

| Ontology | Written by | Covers |
|---|---|---|
| **[MAS](mas.md)** | [norm](../normalization/index.md) | The core execution schema: structural types (`MAS`, `Agent`, `LLM`, `Tool`, `Processing`), their execution instances, and the normalised `State`/`Transition` trajectory |
| **[Semantic](semantic.md)** | [embedding](../../analysis/workers/embedding-worker.md), [grouping](../../analysis/workers/grouping-worker.md), [hierarchical grouping](../../analysis/workers/hierarchical-grouping-worker.md) | The embedding layer, linking State and Execution nodes to embeddings, and the semantic groups that gather sessions |
| **[Metrics](metrics.md)** | [mce](../../analysis/workers/mce-worker.md) | The Metric/MetricResult definition–observation split, score nodes and templates |
| **[Analysis](analysis.md)** | [analysis](../../analysis/workers/analysis-worker.md), [cognitive failure](../../analysis/workers/cognitive-failure-worker.md), [cognition protocols](../../analysis/workers/cognition-protocols-worker.md) | Analysis reports: consistency, anomaly and normal behaviour of a group of sessions, and the cognitive failures and Cognition protocols of a session |
| **[Insight](insight.md)** | [intelligence](../../analysis/workers/intelligence-worker.md) | Rendered, catalog-templated insights attached to a target graph node |

The MAS ontology is the core: the other four are not written by normalisation, and each links
back to its State and Execution nodes. That is what makes the analysis composable. How the layers
fit together is described in [the knowledge graph](../../index.md#the-knowledge-graph).

## SHACL shapes

Knowledge-graph instances are verified against two sets of shapes:

- `mas-shapes-custom.ttl` (committed): hand-maintained constraints that cannot be generated,
  such as SPARQL-based multi-node rules and conditional checks.
- `mas-shapes.ttl` (**generated, not committed**): class-level `sh:NodeShape` scaffolding derived
  from the ontology files, produced at build time together with the Pydantic models of the
  graph's nodes and edges.

## The `scope-ontology` package

The ontologies, the shapes and Python helpers to load, validate and query them are packaged as
`scope-ontology`. In this repository's backend workspace, it is installed from `ontology/`
itself, so a change to a `.ttl` file is picked up by the next `uv sync`.

```bash
pip install scope-ontology
```

The source files are in
[`ontology/src/scope_ontology/`](https://github.com/ioc-project/scope/tree/main/ontology/src/scope_ontology),
and the package's usage is documented in its
[README](https://github.com/ioc-project/scope/blob/main/ontology/PACKAGE_README.md).
