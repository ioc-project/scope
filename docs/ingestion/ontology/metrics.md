# Metrics ontology

<p class="eyebrow">Metric definitions and their results</p>

The definition–observation split for metrics: a `Metric` defines what is measured, and a `MetricResult` holds the value computed for a session or an execution element, linked to the definition (`ofMetricType`) and to the element it measures (`measures`). It also holds score nodes and templates.

- **Written by:** the [mce](../../analysis/workers/mce-worker.md) worker
- **Namespace:** `https://ioc-project.github.io/scope-ontology/mas#` (shared by the five ontologies)
- **Source:** [`metrics-ontology.ttl`](https://github.com/ioc-project/scope/blob/main/ontology/src/scope_ontology/metrics-ontology.ttl)

The metrics computation engine writes one result per computed metric.

Check out the Widoco documentation for the Metrics ontology: every class and property, with
its description, domain and range.

<div class="button-row" markdown>
[Widoco documentation for the Metrics ontology :octicons-link-external-16:](https://ioc-project.github.io/scope/metrics/widoco/){ .md-button .md-button--primary }
[Visualisation (WebVOWL) :octicons-link-external-16:](https://ioc-project.github.io/scope/metrics/webvowl/){ .md-button }
</div>

[:octicons-arrow-right-24: The five ontologies and the graph layers they form](../../index.md#the-knowledge-graph)
