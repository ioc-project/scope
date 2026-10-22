# SCOPE pipelines

SCOPE runs two pipelines on top of the same knowledge graph: an **ingestion pipeline**, triggered once per
completed MAS session, and a set of **periodic operations**, triggered on independent schedules. This page
shows what runs **today**, in the reference deployments, and how each worker is wired. See the
[analysis overview](index.md) for how the pipelines fit into the platform as a whole.

Both pipelines are built from [workers](workers/index.md): standalone, independently deployable Python
packages that share the [`worker-base`](components/worker-base.md) framework. A worker only needs to agree on the
RabbitMQ queue(s) it reads from and writes to, or on the knowledge graph state it reads and writes for periodic
jobs. Nothing else in the pipeline needs to know it exists.

!!! info "What 'today' means on this page"
    The reference deployments are the [Helm chart](https://github.com/ioc-project/scope/tree/main/deploy/k8s/charts/scope)
    and the [Docker Compose stack](https://github.com/ioc-project/scope/tree/main/deploy/docker-compose), which
    deploy the same workers with the same queues. Queue names are settings of each worker: the deployments set
    them explicitly, and the defaults of the CLIs differ in two places, which are noted below.

## Status at a glance

| Worker | Stage | Helm chart | Compose stack | Notes |
|---|---|---|---|---|
| [norm](../ingestion/normalization/norm-worker.md) | Ingestion | :material-check: | :material-check: | Fed by the session-end trigger |
| [embedding](workers/embedding-worker.md) | Ingestion | :material-check: | :material-check: | |
| [mce](workers/mce-worker.md) | Ingestion | :material-check: | :material-check: | Runs in parallel with embedding: nothing is downstream of it |
| [grouping](workers/grouping-worker.md) | Ingestion | :material-check: | :material-check: | |
| [analysis](workers/analysis-worker.md) | Ingestion | :material-check: | :material-check: | Runs anomaly detection, consistency and normal behaviour |
| [stateful-eval](workers/stateful-eval-worker.md) | Ingestion | :material-check: | :material-check: | Fed by embedding |
| [hierarchical-grouping](workers/hierarchical-grouping-worker.md) | Periodic | :material-check: | :material-check: | |
| [intelligence](workers/intelligence-worker.md) | Periodic | :material-check: | :material-check: | |
| [cognitive-failure](workers/cognitive-failure-worker.md) | Ingestion | :material-check: | :material-check: | Fed by embedding. It makes a judge-model call for each session, and can be turned off in both deployments |
| [cognition-protocols](workers/cognition-protocols-worker.md) | Ingestion | :material-check: | :material-check: | Fed by embedding. Its image needs an SSH agent that can read private packages to build, and is not published by CI yet |
| [anomaly-detection](workers/anomaly-detection-worker.md), [consistency](workers/consistency-worker.md), [normal-behaviour](workers/normal-behaviour-worker.md) | Ingestion | :material-minus: | :material-minus: | Images built, but their analyses run inside the analysis worker |

:material-check: deployed · :material-close: not deployed · :material-minus: not deployed on its own

## Ingestion pipeline

It runs once per completed session, and is driven by chained RabbitMQ queues: the output queue of a worker is
the input queue of the next one.

A session starts when it **ends**. The [session-end trigger](https://github.com/ioc-project/scope/blob/main/deploy/docker-compose/scripts/session-end-trigger.sh)
watches the span datastore for `session.end` spans, and publishes the session to the norm worker's queue, exactly
once for each session. A session that never ends is never ingested.

```text
span datastore ── session-end trigger
                        │
                 new_session_to_norm
                        │
                        ▼
                      norm ─────────────────────────┐
                        │                           │
             new_session_to_mce             new_session_to_emb
                        │                           │
                        ▼                           ▼
                       mce                      embedding
                    (metrics)                       │
        ┌─────────────────────┬─────────────────────┴───────────┬────────────────────────┐
        │                     │                                 │                        │
new_session_to_       new_session_to_                 new_session_to_          new_session_to_
   grouping            stateful_eval                  cognitive_failure        cognition_protocols
        │                     │                                 │                        │
        ▼                     ▼                                 ▼                        ▼
    grouping            stateful-eval                   cognitive-failure        cognition-protocols
        │              (trajectory metrics)             (cognitive failure       (protocols used and
new_session_to_                                              reports)             their metrics)
   analysis
        │
        ▼
    analysis
(anomaly ‖ consistency ‖ normal behaviour)
```

| Worker | Input queue | Output queue(s) | What it adds to the knowledge graph |
|---|---|---|---|
| [norm](../ingestion/normalization/norm-worker.md) | `new_session_to_norm` | `new_session_to_mce`, `new_session_to_emb` | The normalised session: MAS, execution and trajectory |
| [mce](workers/mce-worker.md) | `new_session_to_mce` | none | The configured metrics, as `MetricResult` nodes |
| [embedding](workers/embedding-worker.md) | `new_session_to_emb` | `new_session_to_grouping`, `new_session_to_stateful_eval`, `new_session_to_cognitive_failure`, `new_session_to_cognition_protocols` | An embedding for each state |
| [grouping](workers/grouping-worker.md) | `new_session_to_grouping` | `new_session_to_analysis` | The session's membership of a semantic group |
| [analysis](workers/analysis-worker.md) | `new_session_to_analysis` | none | The consistency, anomaly and normal behaviour reports of the group |
| [stateful-eval](workers/stateful-eval-worker.md) | `new_session_to_stateful_eval` | none | Trajectory metrics (opt-in, see the worker) |
| [cognitive-failure](workers/cognitive-failure-worker.md) | `new_session_to_cognitive_failure` | none | A `CognitiveFailureReport` for each cognitive failure detected in the session |
| [cognition-protocols](workers/cognition-protocols-worker.md) | `new_session_to_cognition_protocols` | none | Which Cognition protocols the session's agents had and used, and the metrics of the protocols used |

Four things to know when reading the diagram:

- **MCE is not upstream of grouping.** The MCE worker runs in parallel with embedding and has no output queue.
  Grouping works from the embeddings, so it is fed by the embedding worker alone.
- **Four workers hang off embedding.** Grouping, stateful evaluation, cognitive failure detection and Cognition
  protocol detection all read the session once it has its embeddings, and run in parallel with one another. None
  of the last three has an output queue. The last two make judge-model calls, and each can be turned off: remove
  its queue from the embedding worker's output queues, and stop the worker (in the Helm chart,
  `workers.cognitive-failure.enabled` and `workers.cognition-protocols.enabled`).
- **Analysis needs a group.** `analysis` is a bundle of three independent analyses, run together for each
  semantic group. A session only reaches it if grouping could assign the session to a group. The
  [hierarchical grouping](#periodic-operations) worker also sends groups there when it rearranges them.
- **Queue names differ from the CLI defaults in two places.** The CLIs of the norm and embedding workers read
  `new_session_in` and `new_session_to_embedding`, and the deployments set `new_session_to_norm` and
  `new_session_to_emb`. A worker started by hand, without those settings, uses its defaults.

## Periodic operations

Periodic workers are not chained to the ingestion pipeline by queues. Each one is triggered independently, on its
own schedule, and connects to the rest of the platform through the knowledge graph state it reads and writes. It
is the same modularity principle, applied to time instead of events.

Both periodic workers trigger themselves: they publish a message to their own input queue every
`PERIODIC_TRIGGER_INTERVAL_SECONDS`, and consume it.

| Worker | Self-trigger queue | Interval in the deployments | Reads | Writes |
|---|---|---|---|---|
| [hierarchical-grouping](workers/hierarchical-grouping-worker.md) | `new_session_to_periodic_grouping` | 900 s | Session input embeddings | The semantic-group hierarchy. Groups that changed are sent to `new_session_to_analysis` |
| [intelligence](workers/intelligence-worker.md) | `new_session_to_intelligence` | 300 s | `SemanticGroup`, anomaly and normal behaviour reports, and metrics | `Insight` nodes, rendered from the templates of the [intelligence catalog](components/intelligence-catalog.md) |

The hierarchical grouping worker holds a lock while it reorganises the groups. While it is held, the grouping
worker puts the message back on its queue instead of attaching the session, so that a session is never attached
to a group being rearranged.

## Why the pipelines are modular

Every worker is:

- **A separate package.** It has its own `pyproject.toml`, CLI entry point and `Dockerfile`, and is built and
  deployed on its own, so shipping or rolling back one worker never touches the others.
- **A queue consumer and producer, not a caller.** A worker only knows the names of the queues it reads from and
  writes to. It has no reference to the workers upstream or downstream of it.
- **Runnable two ways.** In production it runs in long-lived RabbitMQ queue mode, and for local testing or
  workflow steps in `--run-once` single-message mode.

The practical consequence is that adding a stage means writing a worker that subscribes to an existing output
queue and publishes its own results, and no existing worker is modified.

## Adding a new worker

- **Ingestion stage.** Subscribe to an existing output queue, publish reports or nodes to the knowledge graph and,
  optionally, a new output queue for a worker further downstream. Then add the new queue to the output queues of
  the worker that should feed it.
- **Periodic job.** Add a worker with its own timer, or its own externally triggered queue, that reads the
  knowledge graph state it needs and writes its results back.
