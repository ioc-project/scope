# Cognition Fabric integration

OXP is the **Observability & Explainability Platform** for the IoC Cognition Fabric. It turns
multi-agent system telemetry into a queryable Knowledge Graph, enriches that graph with analysis
engines, and publishes curated Insights for Cognition Engines and other consumers.

## What OXP does

OXP processes each session through three stages:

1. **Observe and normalize** OTel spans and runtime events into a common MAS execution model.
2. **Enrich** the Knowledge Graph with metrics, embeddings, semantic groups, findings, impact,
	 resource-use analysis, and evaluation results.
3. **Explain** observed behavior through Insights exposed by the Claris API and Intelligence
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

This is a separate Compose stack from SCOPE's default deployment. From the SCOPE repository root:

```bash
cd integrations/cognition-fabric
cp .env.template .env
```

Edit `.env` for your environment. The Analytics API, event exporter, and SCOPE MCE worker build
locally. The other workers still pull their existing GHCR images. Authenticate to GHCR if needed:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <your-github-username> --password-stdin
```

Start the stack:

```bash
docker compose up --build
```

Override the registry, tag, or integration-specific MCE config as needed:

```bash
OXP_IMG_TAG=1.2.3 docker compose up               # pin a specific version
CLARIS_REGISTRY=my.registry/path docker compose up # use a mirror
MCE_CONFIG_FILE=/path/to/mce_config.yaml docker compose up
```

The `mce` worker and [Analytics API](ioc-analytics-api/README.md) share the implementation in
[SCOPE's MCE packages](../../backend/mce). The worker reads
[`mce_config.yaml`](mce_config.yaml) for integration-specific metric and engine settings.
The Analytics API does not read that file.

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
