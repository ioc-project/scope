# Cognition Fabric integration

SCOPE provides observability and explainability for IoC Cognition Fabric. It turns
multi-agent system telemetry into a queryable knowledge graph, enriches that graph with analysis
engines, and publishes curated insights for Cognition Engines and other consumers.

## What SCOPE does

SCOPE processes each session through three stages:

1. **Observe and normalize** OTel spans and runtime events into a common MAS execution model.
2. **Enrich** the Knowledge Graph with metrics, embeddings, semantic groups, findings, impact,
	 resource-use analysis, and evaluation results.
3. **Explain** observed behavior through insights exposed by the Analytics API and Intelligence
	 Catalog.

The Knowledge Graph is the shared spine of the platform. Pipeline stages write enriched data to it,
periodic operations train models over accumulated data, and consumers read the resulting graph or
its curated Insights.

## Core capabilities

- Multi-agent telemetry ingestion and normalization
- Knowledge Graph storage using Neo4j
- Semantic grouping and behavior analysis
- Anomaly, consistency, and normal-behavior detection
- Impact assessment and wasted-resource inference
- Stateful and neuro-symbolic evaluation
- Realtime topology and runtime-event observability

## Local setup

This is a separate Compose stack from SCOPE's default deployment. Its project name is
`scope-cognition-fabric`, so Compose generates container names such as
`scope-cognition-fabric-ioc-analytics-api-1`. From the SCOPE repository root:

```bash
cd integrations/cognition-fabric
cp .env.template .env
```

Edit `.env` to point to a ClickHouse instance. Compose builds the Analytics API and event
exporter locally, starts RabbitMQ and Neo4j, and pulls the `norm` worker image from GHCR.
Authenticate to GHCR if needed:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <your-github-username> --password-stdin
```

Start the stack:

```bash
docker compose up --build
```

Override the registry or tag for the prebuilt `norm` worker as needed:

```bash
OXP_IMG_TAG=1.2.3 docker compose up               # pin a specific version
CLARIS_REGISTRY=my.registry/path docker compose up # use a mirror
```

The [Analytics API](ioc-analytics-api/README.md) still uses
[SCOPE's MCE packages](../../backend/mce) to compute metrics on demand. This stack does not
run background enrichment workers or a Redis cache.

Compose builds `event-exporter` locally and exposes its OTLP/gRPC endpoint on port `4417`, leaving
`4317` available for the OTel Collector. Collectors on the Compose network can send logs to
`event-exporter:4417`; collectors on the host can use `localhost:4417`. Set
`EVENT_EXPORTER_CONFIG_FILE` to mount a different rules file.
The shipped rules use `localhost:9100` for callbacks, which refers to the exporter container in
Compose; use a reachable callback address in your rules file to deliver notifications.

## Container image releases

Run [Image Builder for Cognition Fabric](../../.github/workflows/cognition-fabric-main.yaml)
manually to build both images without pushing. Pushing a `cognition-fabric-v*` tag
(for example, `cognition-fabric-v1.2.3`) publishes both the version (`1.2.3`)
and `latest` tags to GHCR:

- `ghcr.io/ioc-project/container-images/scope-cognition-fabric-event-exporter`
- `ghcr.io/ioc-project/container-images/scope-cognition-fabric-ioc-analytics-api`

The local Compose stack still builds these two images from source.

## Status

**MVP delivered; modularizing toward OSS.** The MVP includes the multi-layer Knowledge Graph,
semantic analysis, detection, impact assessment, wasted-resource analysis, stateful and
neuro-symbolic evaluation, and realtime observability.
