# SCOPE on Docker Compose

Runs the whole SCOPE stack on one machine, with the same components as the
[`k8s/charts/scope`](../k8s/charts/scope) umbrella chart.

| Compose service | Chart equivalent |
|---|---|
| `gateway` | `gateway` (nginx, routes `/gql` and `/api/*` to the API, the rest to the UI) |
| `ui`, `api`, `redis` | `scope-ui`, `scope-api` subcharts and the API cache |
| `otel-collector` | `opentelemetry-collector` subchart + `otel-collector-configmap.yaml` |
| `clickhouse`, `clickhouse-setup` | `clickhouse.yaml`, `clickhouse-init-job.yaml` |
| `session-end-trigger` | `session-end-trigger.yaml` |
| `rabbitmq`, `neo4j` | `rabbitmq.yaml`, `neo4j` subchart |
| `norm`, `embedding`, `mce`, `grouping`, `hierarchical-grouping`, `intelligence`, `analysis`, `stateful-eval`, `cognitive-failure`, `cognition-protocols` (see below) | `worker-*.yaml` |

Kubernetes-only concerns (ingress, HPA, cert-manager, ExternalSecrets, resource limits) have no
equivalent here.

## Requirements

- Docker with Compose v2.17+ and BuildKit.
- A checkout of the whole repository: the images are built from `../../backend`, `../../ontology`
  and `../../ui`.
- Memory: give Docker at least 12 GB. The chart's worker limits go up to 12 GB each.
- An OpenAI-compatible LLM endpoint and a text-embedding model.

## Run

```bash
cd deploy/docker-compose
cp .env.template .env      # set LLM_MODEL_NAME, EMBEDDING_MODEL_NAME, LLM_GATEWAY_* and the passwords
docker compose up -d --build
```

`--build` builds the images from this checkout. Without it Compose pulls
`ghcr.io/ioc-project/container-images/scope-*:${SCOPE_VERSION}`, so those tags must exist in the
registry (override `SCOPE_IMAGE_REGISTRY` / `SCOPE_VERSION` in `.env` otherwise).

| What | Where |
|---|---|
| UI and API | http://localhost:8080 (`SCOPE_PORT`) |
| OTLP ingestion | `localhost:4317` (gRPC), `localhost:4318` (HTTP) |
| RabbitMQ management | http://localhost:15672 |
| Neo4j browser | http://localhost:7474 |

Only the gateway and the OTLP ports listen on all interfaces; RabbitMQ and Neo4j are bound to
`127.0.0.1` (change the host ports in `.env` if they clash). ClickHouse is not published; use
`docker compose exec clickhouse clickhouse-client`.

Point your instrumented application at the collector, for example
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`. When a session ends, the `session-end-trigger`
publishes it to the `norm` worker and the pipeline takes over.

## Configuration

- `.env`: image tag, LLM settings, credentials, published ports and UI feature flags (see
  `.env.template`).
- `config/mce_config.yaml`, `config/stateful_eval_config.yaml`, `config/cognitive_failure_config.yaml`: the
  worker configs, mirroring `workers.mce.config`, `workers.stateful-eval.config` and
  `workers.cognitive-failure.config` of the chart's `values.yaml`.
- The `cognitive-failure` worker makes a judge-model call for each session. To turn it off, run
  `docker compose stop cognitive-failure` and set `EMBEDDING_OUTPUT_QUEUE` in `.env` without its queue (see
  `.env.template`).
- Queue names and worker tuning (inflight messages, grouping distances, trigger intervals) are set
  in `docker-compose.yml` with the chart's default values.
- `scripts/` holds the two shell scripts that the chart inlines in its Job and Deployment; keep
  them in sync with the chart when it changes.

### Cognition protocols worker

The `cognition-protocols` worker detects which Cognition protocols (ACCORD, CONCORD) a session used, and
evaluates the ones it used. It is part of the stack by default, but its image has to be built with an SSH agent
that can read the private protocol packages: `docker compose up -d --build` forwards your agent
(`ssh: [default]` in the compose file), so `ssh-add -l` must list a key with access to those repositories, and
Docker may ask you to allow the forwarding (`--allow=ssh`). This setup has not been tested end to end.

To run the stack without it, stop the service (`docker compose stop cognition-protocols`), and set
`EMBEDDING_OUTPUT_QUEUE` in `.env` without `new_session_to_cognition_protocols` (see `.env.template`). Without a
key, the build of this one service fails.

Data lives in the named volumes `clickhouse-data`, `rabbitmq-data`, `neo4j-data` and `neo4j-logs`.
`docker compose down -v` deletes it.
