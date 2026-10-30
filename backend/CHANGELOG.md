# Changelog

All notable changes to the SCOPE backend will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The `mce` worker computed one session at a time.** `MCEWrapper.process_mce_session` was `async` but called the blocking
  evaluation directly, which held the event loop: whatever `MAX_INFLIGHT_MESSAGES` said, sessions went through one after the
  other, about 8 seconds each with the default metrics. It now runs the evaluation in a thread, and the engine that the sessions
  share is built once. A burst of 33 sessions had its metrics in 68 seconds instead of 260.
- **Workers were capped at 16 sessions at a time, below their `MAX_INFLIGHT_MESSAGES`.** The handlers hand their blocking work to
  `asyncio.to_thread`, whose pool holds `min(32, cpus + 4)` threads (16 on a 12-CPU host), so a worker allowed 32, 64 or 128
  messages in flight never ran more than 16. `BaseWorker` now sizes the pool to `max_inflight_messages`.
- **`Connection pool is full, discarding connection: clickhouse` in the logs.** The ClickHouse client of the workers shared
  clickhouse-connect's default pool of 8 HTTP connections, and the `norm` worker, which reads the spans of many sessions
  at once, opened more and threw the extra ones away after each request. `ClickHouseConnector` now has its own pool,
  of 32 connections by default (`CLICKHOUSE_POOL_SIZE` sets it).
- **Neo4j writes no longer lose a session to a deadlock.** When several workers wrote the same shared nodes at once (a
  burst of sessions writing the same agents, tools and model), Neo4j rolled one of the transactions back with a
  transient error (`DeadlockDetected`), and the `norm` worker dropped the whole session. `Neo4JConnector` now tries a query
  again, up to five times with a short random wait, when Neo4j reports a transient error. The same burst on a new
  database could also fail on the index that two sessions create at the same time
  (`EquivalentSchemaRuleAlreadyExists`): that error is now what it says, the index exists.
- **Normalization: identical boundary States are one State.** When a call started with what the previous step
  produced (a tool called with its agent's input, a tool returning what its agent returns, a handoff that passes
  its output on unchanged), `norm` kept two State nodes with the same content and no edge between them, so the
  chain from a session's input to its final state could not be followed through `inputTo` / `leadsTo`. They are
  now merged into one State (the outer element's own State is kept at an agent's boundary), and only States whose
  content differs are still bridged by a `ProcessingCall`. Sessions already in the knowledge graph keep their old
  States until they are normalized again.

### Added

- **Cognitive failure worker.** `cognitive-failure-worker` detects the cognitive failures of the challenges
  taxonomy (IR-1, CC-3, ...) in a session and writes a `CognitiveFailureReport` (failure code,
  `low`/`medium`/`high` confidence, reasoning) attached to the session by `reportsOn` and to the call where it
  happened by `atCall`. The worker fetches the trajectory, writes it to a temporary file that is deleted
  afterwards, and an agent without access to the API or the graph reads it with `list_steps` / `read_step` /
  `grep` tools and answers through a schema-enforced `submit_detections` tool. Opt-in: list the failures to look for in `cognitive_failure_config.yaml`
  (`workers.cognitive-failure.config` in the Helm chart). It is fed from the embedding worker's output, next to
  the stateful-eval worker. The Cypher is in `scope.query_builders.failure_graph`, the writer in
  `scope.client.failures`. Requires a `scope-ontology` release with `CognitiveFailureReport` without `reportedAt`.
  A report's `name` is the failure's taxonomy name, its reasoning is a developed explanation that cites steps by
  description and span, and `get_session_trajectory` steps now carry the OTel `span_id`.
- **Cognition protocol reports.** `scope.providers.SCOPECognitionProtocolReportProvider` writes, through the ontology models, an
  `CognitionProtocolReport` per session (`protocolsEnabled` / `protocolsActivated`: which Cognition protocols were available and
  which were used) and a `CognitionProtocolExecutionReport` per session and protocol (the agents and skills that had
  it and used it, and its evaluation metrics), both linked to the `Session` by `reportsOn`.
  `save_protocol_execution_metrics` stores a protocol's metrics as `MetricResult` nodes that `measure` its
  execution report (the report's earlier results are replaced), via the new `protocol_execution_report` target
  of `metric_graph.write_metric_result_query`. The Cypher is in `scope.query_builders.cognition_protocol_graph`.
  Requires `scope-ontology>=1.0.6`.
- **Cognition protocols read API.** `GET /sessions/{id}/cognition-protocols` reads the session's `CognitionProtocolReport` and
  `CognitionProtocolExecutionReport` nodes and returns, per protocol (`ACCORD`...), `enabled` / `activated`, the
  agents and skills that had / used it, and a generic `metrics` list (`name`, `section`, `value`, `reasoning`
  or parsed `details`, `provider`, `unit`, `timestamp`). `sessions-with-cognition-protocols` returns each session's
  enabled / activated protocols from its `CognitionProtocolReport`; its `concord` summary is no longer filled.

### Changed

- **Workers can run in one process, and Compose runs three services instead of eight.** `worker_base.runner` starts the
  workers of a group in threads of one process, each with its own event loop, queues and options, so the interpreter and
  the libraries they share are loaded once. The new images are `ingestion-worker` (norm, embedding),
  `semantic-analysis-worker` (grouping, hierarchical-grouping, analysis, intelligence) and `eval-worker` (mce,
  stateful-eval); the analysis worker, which forks a pool of CPU-bound processes, runs as a child process of its group.
  `--workers` (or `INGESTION_WORKERS`, `SEMANTIC_ANALYSIS_WORKERS`, `EVAL_WORKERS`) picks some of them, and the settings
  that all the workers read from one variable can be set per worker with its prefix (`NORM_MAX_INFLIGHT_MESSAGES`,
  `HIERARCHICAL_GROUPING_PERIODIC_TRIGGER_INTERVAL_SECONDS`; `scoped_getenv` in `worker_base.utils`), and
  `<WORKER>_ARGS` adds command line options. The single-worker images and CLIs are unchanged (and still needed for Argo
  run-once mode); the Helm chart still deploys the single workers. On a 33-session replay the three services took 1.6 GB
  against 2.0 GB for the eight containers, and the pipeline finished in 85 s against 101 s.
- **The MCE can ignore the metric plugins of some packages (`MCE_IGNORED_PLUGIN_PACKAGES`).** It loads every package that
  registers a metric in the `metrics_computation_engine.plugins` entry-point group and skips the ones that are not MCE
  metrics, but loading is importing: in an image that also has the stateful evaluation (`eval-worker`), the MCE imported
  `mdt-metrics` and `litellm` with it, which took its process from 102 MB to 388 MB after discovery. The `eval-worker` image
  sets it to `mdt-metrics`; the metric catalog is the same (45 metrics).
- **The stateful evaluation calls the judge model with the OpenAI SDK, not LiteLLM.** `metrics-computation-engine`'s
  `LLMClient` is a thin wrapper around `litellm.completion`, and importing it (which `BaseMetric` and `Jury` do) loaded
  LiteLLM, about 300 MB per process. `stateful_evals_be.llm_client.LLMClient` has the same constructor and `query`
  signature and calls the `openai` SDK (Azure OpenAI when `api_version` is set); it replaces the package's module when
  `stateful_evals_be` is imported. A parameter the endpoint refuses (`reasoning_effort`, `temperature`, `response_format`)
  is dropped and the call retried, as LiteLLM's `drop_params` did. LiteLLM is still installed, since
  `metrics-computation-engine` depends on it, but no longer imported.
- **Insights from the cognitive failure detections.** Two intelligence-catalog templates read the
  `CognitiveFailureReport` nodes: one insight per failure and session (priority from the detector's confidence), and one
  per failure and semantic group when it is recurring (at least 2 sessions and 30% of the group, medium or high
  confidence).
- **Fixed: the intelligence worker failed with `Insight failed ontology validation: concerns: Less than 1 values`.**
  `ingest_insights` validated each insight without its `concerns` edge to the node it is attached to, which the
  ontology requires, so no insight could be written. It now passes that edge.
- **`cognition-protocols-worker` depends on `ioc-scope-mce-core`, not the `ioc-scope-mce` meta package.** It only runs the
  ACCORD and CONCORD plugin metrics, but the meta package installs every provider (native, sdk, deepeval, opik, ragas,
  legacy), with `litellm`, `pyarrow`, `scipy`, `scikit-learn` and `pandas` behind them. The image's virtual environment goes
  from 788 MB to 233 MB; the same 49 protocol metrics are discovered.
- **`clickhouse-sqlalchemy` is an optional extra of `ioc-scope-api`.** Nothing imports it (span reads use
  `clickhouse-connect`), but it pulled `asynch` (66 MB) into every worker image. The API image still installs it
  (`clickhouse-sqlalchemy` extra), for a `SQLAlchemyConnector` pointed at a ClickHouse URL.
- **`umap-learn` and `numba` are only installed in the API image.** They serve the UMAP projection of
  `GET /sessions/{id}/latent-space`, but `ioc-scope-api` is also the knowledge-graph client of every worker, so all the worker
  images carried them, with llvmlite, scikit-learn and scipy behind them. They are now the `latent-space` extra of
  `ioc-scope-api`, which the API Dockerfile installs (`uv sync --extra latent-space`). The norm worker image goes from
  970 MB to 494 MB, and no worker image has numba, llvmlite or umap any more (three Dockerfiles no longer need their
  `--no-install-package numba` workaround). Without the extra, the latent-space endpoint answers 501 and names it.
- **The embedding worker image no longer installs torch.** `dem.embedding` imported torch, transformers,
  sentence-transformers, gensim and google-genai as soon as it was imported, so the worker paid about 360 MB of memory for
  libraries that `OpenAIEmbedder` (the embedder of the Compose file and the Helm chart) never uses. Each embedder now imports
  its library when it is built. `dem[embedding]` is the API embedders only (openai, google-genai); the local models
  (`SentenceTransformerEmbedder`, `Qwen3Embedder`, `GensimEmbedder`) moved to the new `dem[embedding-local]` extra, and the
  embedding worker has a matching `local` extra, which `Dockerfile.gpu` installs. The default image goes from 1.54 GB to
  0.72 GB and the process from about 470 MB to about 105 MB. A local embedder without its libraries fails with a message
  that names the extra. To use one in the default image, build a variant with `--extra local`.
- **The cognition-protocols image builds without an SSH agent.** The ACCORD and CONCORD metric packages are read from the
  public `ioc-project/cognition-protocols` repository over HTTPS (`ssh: [default]` is gone from the Compose file and the bake
  target), and the image is part of the CI matrix like the others.
- **Breaking: metrics are stored as the ontology's `Metric` + `MetricResult`.** A `MetricResult`
  (`result`, `reasoning`, `timestamp`, `evaluation*` cost fields) is linked to its `Metric` definition
  (one per metric name and provider, with `metricProvider`, `unit`, `valueRange`) by `ofMetricType`, and
  `measures` the `Session`, or the call (by span id), it was computed for. This replaces the
  `(:Session|:Span)-[:hasMetric]->(:Metric {metricName, metricResult, value, provider, source,
  metricId, ...})` nodes: there is no `hasMetric` edge and no `Span` node for metrics, and `source` and
  `metricId` are gone (the provider identifies the producer). Every reader (API queries, group-worker
  data, UI rollups, insight templates) and the writers (MCE, stateful-eval worker) use the new model;
  there is no backward compatibility, so existing metric nodes must be deleted and recomputed.
- `aboutMetric` links from consistency, anomaly and normal-behaviour reports now point at the `Metric`
  definition(s) of the metric name (they never matched anything before).
- Requires `scope-ontology` with `MetricResult.timestamp`.
- The stateful-eval worker can run the trajectory metrics (Policy Safety, Task Completion, ...) and writes each
  as a session `MetricResult`. They are opt-in: list the ones to run in `stateful_eval_config.yaml` (`workers.stateful-eval.config` in the
  Helm chart); with no file or an empty list none run.
- The LLM price table behind the evaluation cost is refreshed from LiteLLM on every `mce-worker` and
  `stateful-eval-worker` image build (in the Dockerfile, so local builds too) and at run time, falling back to
  the table packaged in the image. See `MODEL_PRICES_REFRESH`, `MODEL_PRICES_URL` and `MODEL_PRICES_TTL_SECONDS`.

## [1.0.0] - 2026-09-30

### Added

Initial open-source release of the SCOPE backend.

- **[api](../docs/platform/api/index.md)** — unified DB access layer across raw OTel spans
  (ClickHouse/SQLite/PostgreSQL) and the knowledge graph (Neo4j), with an optional FastAPI REST
  server on top.
- **[dem](../docs/analysis/components/analysis.md)** ("analysis") — anomaly detection, consistency checking,
  embeddings, semantic grouping, and lightweight LLM integration over metric, textual, and graph
  representations of MAS executions.
- **[norm](../docs/ingestion/normalization/index.md)** — normalizes raw OTel traces into structured MAS execution
  models following the [MAS ontology](../ontology).
- **[mce](../docs/analysis/components/mce.md)** — Metrics Computation Engine: a pluggable framework computing
  quantitative and LLM-judged metrics for MAS sessions (`Duration`, `Cost`, `AnswerRelevancy`,
  `Groundedness`, and more).
- **[stateful-evals](../docs/analysis/components/stateful-evals.md)** — cost-effective stateful evaluation of
  a session's trajectory.
- **[intelligence-catalog](../docs/analysis/components/intelligence-catalog.md)** — catalog of insight
  templates rendered into `Insight` knowledge-graph nodes.
- **[worker-base](../docs/analysis/components/worker-base.md)** — shared RabbitMQ worker framework
  (`BaseWorker`) used by every standalone worker.
- 11 standalone, independently deployable [workers](../docs/analysis/workers/index.md) built on
  `worker-base`, covering the ingestion pipeline (`norm-worker` → `mce-worker` /
  `embedding-worker` → `grouping-worker` → `analysis-worker`) and periodic jobs
  (`hierarchical-grouping-worker`, `intelligence-worker`) — see the
  [pipelines documentation](../docs/analysis/pipelines.md).
- Local deployment via Docker Compose ([`docker-compose.yml`](docker-compose.yml)), bringing up
  the OTel collector, ClickHouse, Neo4j, RabbitMQ, and the full ingestion pipeline.
- Kubernetes deployment via the Helm charts under [`deploy/k8s`](../deploy/k8s).
