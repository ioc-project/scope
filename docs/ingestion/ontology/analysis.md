# Analysis ontology

<p class="eyebrow">Analysis reports</p>

The reports computed by the analysis workers. Group-scoped reports are computed over a `SemanticGroup` of sessions: consistency, anomaly and normal behaviour. Session-level reports describe one session: its cognitive failures (`CognitiveFailureReport`) and the Cognition protocols its agents had and used (`CognitionProtocolReport` and `CognitionProtocolExecutionReport`). They are `mas:AnalysisElement` companions of the MAS ontology.

- **Written by:** the [analysis](../../analysis/workers/analysis-worker.md), [cognitive failure](../../analysis/workers/cognitive-failure-worker.md) and [cognition protocols](../../analysis/workers/cognition-protocols-worker.md) workers
- **Namespace:** `https://ioc-project.github.io/scope-ontology/mas#` (shared by the five ontologies)
- **Source:** [`analysis-ontology.ttl`](https://github.com/ioc-project/scope/blob/main/ontology/src/scope_ontology/analysis-ontology.ttl)

The analysis worker runs the three group analyses over the group a session was assigned to. The two other workers write one report per session, and per protocol for the Cognition protocols.

Check out the Widoco documentation for the Analysis ontology: every class and property, with
its description, domain and range.

<div class="button-row" markdown>
[Widoco documentation for the Analysis ontology :octicons-link-external-16:](https://ioc-project.github.io/scope/analysis/widoco/){ .md-button .md-button--primary }
[Visualisation (WebVOWL) :octicons-link-external-16:](https://ioc-project.github.io/scope/analysis/webvowl/){ .md-button }
</div>

[:octicons-arrow-right-24: The five ontologies and the graph layers they form](../../index.md#the-knowledge-graph)
