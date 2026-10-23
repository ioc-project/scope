# MAS ontology

<p class="eyebrow">The core execution schema</p>

The schema the knowledge graph follows. It describes the **structural** types of a multi-agent system (`MAS`, `Agent`, `LLM`, `Tool`, `Processing`), their **execution** instances (`Session`, `MASCall`, `AgentCall`, `LLMCall`, `ToolCall`, ...), and the normalised **trajectory** of a session as a chain of `State` and `Transition` nodes.

- **Written by:** the [norm](../normalization/index.md) worker
- **Namespace:** `https://ioc-project.github.io/scope-ontology/mas#` (shared by the five ontologies)
- **Source:** [`mas-ontology.ttl`](https://github.com/ioc-project/scope/blob/main/ontology/src/scope_ontology/mas-ontology.ttl)

Normalisation expresses a session's spans in these terms; every other ontology links back to its nodes.

Check out the Widoco documentation for the MAS ontology: every class and property, with
its description, domain and range.

<div class="button-row" markdown>
[Widoco documentation for the MAS ontology :octicons-link-external-16:](https://ioc-project.github.io/scope/mas/widoco/){ .md-button .md-button--primary }
[Visualisation (WebVOWL) :octicons-link-external-16:](https://ioc-project.github.io/scope/mas/webvowl/){ .md-button }
</div>

[:octicons-arrow-right-24: The five ontologies and the graph layers they form](../../index.md#the-knowledge-graph)
