# Insight ontology

<p class="eyebrow">Rendered, catalog-templated insights</p>

The insights attached to a target node of the graph (MAS, Agent, Session or SemanticGroup). An insight is rendered from a knowledge-graph query and an `InsightTemplate` of the catalog. Insights are also `mas:AnalysisElement`s.

- **Written by:** the [intelligence](../../analysis/workers/intelligence-worker.md) worker
- **Namespace:** `https://ioc-project.github.io/scope-ontology/mas#` (shared by the five ontologies)
- **Source:** [`insight-ontology.ttl`](https://github.com/ioc-project/scope/blob/main/ontology/src/scope_ontology/insight-ontology.ttl)

The intelligence worker executes the catalog's templates and writes the resulting insights.

Check out the Widoco documentation for the Insight ontology: every class and property, with
its description, domain and range.

<div class="button-row" markdown>
[Widoco documentation for the Insight ontology :octicons-link-external-16:](https://ioc-project.github.io/scope/insight/widoco/){ .md-button .md-button--primary }
[Visualisation (WebVOWL) :octicons-link-external-16:](https://ioc-project.github.io/scope/insight/webvowl/){ .md-button }
</div>

[:octicons-arrow-right-24: The five ontologies and the graph layers they form](../../index.md#the-knowledge-graph)
