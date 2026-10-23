# SCOPE: System for Cognitive Observability Provenance and Explainability

The System for Cognitive Observability Provenance and Explainability (SCOPE) is a modular platform for analyzing and visualizing Multi-Agent System (MAS) executions from OpenTelemetry traces. It relies on a multi-layers knowledge graph, which holds representations of the different sessions (runs) of a given MAS that were observed previously.

The lifecycle of one MAS session within SCOPE is the following:
1. The MAS sends OTel spans to an OTel collector, which, in turn, sends these spans to a storage place (currently using ClickHouse DB, but it can be replaced by another one quite easily).
2. When the trace is complete, the SCOPE ingestion pipeline is triggered and the Otel spans are mapped to an ontology modeling MAS execution.
3. The representation of the session is then enriched via the different analysis modules.
4. Through the enrichment of each session of a MAS, SCOPE is able to provide explainability and insights about the inner workings of the MAS.

The current analysis offers semantic grouping of the sessions, along with anomaly detection, consistency checking, and stateful evaluation of one session trajectory.
A detailed description of each analysis feature is provided in the [documentation](../docs/analysis/index.md).

## Structure of the repository

This repository provides libraries for the different features and a `workers` folder, which contains the code of standalone workers, built on top of the different libraries.

The subsequent sections provide a quick description of each library.

### api
This library handles the connectivity to the DBs (there is currently two DBs: one for raw OTel spans, and one for the knowledge graph).
It abstracts the DBs and provides endpoints to fetch data from either the raw OTel spans or from the knowledge graph.

### norm
This library transforms (normalizes) the raw OTel traces data into structured MAS execution models, following our ontology.

### analysis
This library provides advanced analytics capabilities including anomaly detection, consistency checking, embeddings, semantic grouping, and LLM integration.
Supports analyzing both metrics and textual data from MAS executions.

### mce
This library provides a metric computation engine, to compute metrics for a given session, or set of sessions.

### stateful-evals
This library computes the stateful evaluation, a cost-effective evaluation of the trajectory of a given session.

## Development Setup

### Quick Start (All Packages)

```bash
# Clone the repository
git clone <repository-url>
cd scope-lib
uv sync
```

### Deployment

The whole SCOPE stack (OTel collector, ClickHouse, RabbitMQ, Neo4j, the ingestion and analysis workers, the API and
the dashboard) runs locally with the Docker Compose stack in [`deploy/docker-compose`](../deploy/docker-compose/README.md).
A Helm chart is provided to deploy it on Kubernetes ([`deploy/k8s`](../deploy/k8s)).

### Running Tests

```bash
# Run all tests across all packages (recommended)
uv run pytest -v
```

### Code Formatting and Linting

```bash
# Format and lint all packages (recommended)
uv run ruff format
uv run ruff check --fix

# Or format/lint each package independently - Example:
cd <package> && uv run ruff format && uv run ruff check --fix
```

## Package Documentation

XXX TODO update XXX
- **api**: See [dal/README.md](dal/README.md) for API usage
- **dem**: See [dem/README.md](dem/README.md) for anomaly detection, embeddings, and analytics
- **norm**: See [norm/README.md](norm/README.md) for entity structures and analysis
