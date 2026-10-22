# Semantic ontology

<p class="eyebrow">The embedding and semantic-group layer</p>

The semantic layer of the trajectory: `SemanticElement` and `Embedding` nodes with the links from `State` and execution nodes to their embeddings, and the `SemanticGroup` nodes that gather sessions sharing a topic, which can be nested into a hierarchy. It is versioned independently from the other four ontologies.

- **Written by:** the [embedding](../../analysis/workers/embedding-worker.md), [grouping](../../analysis/workers/grouping-worker.md) and [hierarchical grouping](../../analysis/workers/hierarchical-grouping-worker.md) workers
- **Namespace:** `https://ioc-project.github.io/scope-ontology/mas#` (shared by the five ontologies)
- **Source:** [`semantic-ontology.ttl`](https://github.com/ioc-project/scope/blob/main/ontology/src/scope_ontology/semantic-ontology.ttl)

The embedding worker computes a vector for each state. The grouping worker then uses the embeddings to attach the session to a semantic group, and the hierarchical grouping worker periodically builds and rearranges the groups.

Check out the Widoco documentation for the Semantic ontology: every class and property, with
its description, domain and range.

<div class="button-row" markdown>
[Widoco documentation for the Semantic ontology :octicons-link-external-16:](https://ioc-project.github.io/scope/semantic/widoco/){ .md-button .md-button--primary }
[Visualisation (WebVOWL) :octicons-link-external-16:](https://ioc-project.github.io/scope/semantic/webvowl/){ .md-button }
</div>

[:octicons-arrow-right-24: The five ontologies and the graph layers they form](../../index.md#the-knowledge-graph)
