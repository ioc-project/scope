# System for Cognitive Observability Provenance and Explainability (SCOPE)

SCOPE is a modular platform for observing, analyzing, and explaining the executions of Multi-Agent Systems (MAS) from OpenTelemetry traces. It ingests raw OTel spans, maps them onto a shared MAS ontology, and enriches each session with analysis such as semantic grouping, anomaly detection, consistency checking, and stateful trajectory evaluation — surfaced through a web dashboard.

## How it works

1. A MAS sends OTel spans to an OTel collector, which stores them (ClickHouse by default, swappable for another backend).
2. Once a trace is complete, the SCOPE ingestion pipeline maps the raw spans onto the [MAS ontology](ontology), producing a structured execution model.
3. The session representation is enriched by the analysis workers (semantic grouping, anomaly detection, consistency checking, stateful evaluation, metrics).
4. The enriched knowledge graph is served through the [API](backend/api) and visualized in the [UI](ui), giving explainability and insight into the MAS's inner workings.

## Repository structure

| Path | Description |
| --- | --- |
| [`backend/`](backend) | Python libraries and workers implementing ingestion, normalization, analysis, and the data API. See [backend/README.md](backend/README.md). |
| [`backend/api`](backend/api) | Unified DB access layer and REST API over the raw OTel spans and the knowledge graph. |
| [`backend/norm`](backend/norm) | Normalizes raw OTel traces into structured MAS execution models. |
| [`backend/mce`](backend/mce) | Metric computation engine for sessions. |
| [`backend/stateful-evals`](backend/stateful-evals) | Cost-effective evaluation of a session's trajectory. |
| [`backend/workers`](backend/workers) | Standalone workers built on top of the backend libraries. |
| [`ui/`](ui) | React/TypeScript web dashboard for browsing applications, sessions, execution graphs, and live topology. See [ui/README.md](docs/platform/ui/index.md). |
| [`ontology/`](ontology) | The MAS telemetry ontology (Turtle/OWL) and the `scope-ontology` Python package. See [ontology/README.md](ontology/README.md). |
| [`docs/`](docs) | Documentation site sources (MkDocs), built with `mkdocs.yml`. |
| [`deploy/`](deploy) | Deployment: Helm charts for Kubernetes (`deploy/k8s`) and a Docker Compose stack (`deploy/docker-compose`) running the API, UI, workers and supporting infra. |

## Getting started

Each component has its own setup instructions:

- Backend (API, workers, local Docker Compose stack): [backend/README.md](backend/README.md)
- UI (dashboard): [ui/README.md](docs/platform/ui/index.md)
- Ontology (Python package, docs, versioning): [ontology/README.md](ontology/README.md)
- Kubernetes deployment: [deploy/k8s/charts/scope/README.md](deploy/k8s/charts/scope/README.md)

For a fully local stack, see the [Docker Compose deployment](deploy/docker-compose/README.md), which runs the same components as the Helm chart: OTel collector, ClickHouse, RabbitMQ, Neo4j, the ingestion pipeline, the API and the UI.

## Documentation

The documentation site is built from [`docs/`](docs/index.md) with `mkdocs.yml` and published at [ioc-project.github.io/scope](https://ioc-project.github.io/scope/):

- **Overview**: [what SCOPE is and how it works](docs/index.md).
- **Ingestion**: [the telemetry path](docs/ingestion/index.md), [normalization](docs/ingestion/normalization/index.md) and [the ontology](https://ioc-project.github.io/scope/ontologies.html).
- **Analysis**: [the analysis engines](docs/analysis/index.md), [pipelines](docs/analysis/pipelines.md), [workers](docs/analysis/workers/index.md) and [components](docs/analysis/components/index.md).
- **Platform**: [the platform](docs/platform/index.md), [API](docs/platform/api/index.md) and [UI](docs/platform/ui/index.md).
- **Tutorials**: [instrument your app](docs/tutorials/instrument-your-app.md) and [deploy SCOPE](docs/tutorials/deploy-scope.md).
- Preview locally: `pip install -r docs/requirements.txt && mkdocs serve`.

The ontology reference documentation (Widoco and WebVOWL) is generated separately:

- **Ontology**: [ontology/index.md](ontology/index.md), also published at [ioc-project.github.io/scope/ontologies.html](https://ioc-project.github.io/scope/ontologies.html)

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines, [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards, and [SECURITY.md](SECURITY.md) for reporting vulnerabilities. Maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md).

## License

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE) for the full text.
