# Cognition Protocols Worker

Detects which Cognition protocols the agents of a session had and used, records that in the knowledge
graph, and evaluates the protocols that were used.

**Source:** [`workers/cognition-protocols-worker`](https://github.com/ioc-project/scope/tree/main/backend/workers/cognition-protocols-worker)

## How it works

For each message (`BaseQueueMessage` with a `session_id`), `CognitionProtocolsWorker.handle_message`
runs the following steps. The blocking graph and MCE calls run off the event loop.

1. **Detects** the protocols. The session's spans (agent, LLM and tool calls with their content) are read
    from the knowledge graph through `SCOPEKGProvider`, and one **detector** per protocol looks at them. For
    each protocol, the detector reports two things:

    - **enabled**: the protocol was available to at least one agent of the session;
    - **activated**: an agent actually used it.

    It also reports which agents and skills had it and which used it.

2. **Records** what was found. A `CognitionProtocolReport` lists the protocols enabled and the protocols
    activated. It is written even when there are none (two empty lists), which records that the session
    was checked. Each *activated* protocol also gets a `CognitionProtocolExecutionReport` with the agents and
    skills that had the protocol and used it. A protocol that was only enabled gets none: there was no
    execution to report on. If the session is not in the graph, nothing is written and the message is reported
    as failed.
3. **Evaluates** every protocol found *activated* that has an **evaluator**. It computes the protocol's metrics
    with the [Metrics Computation Engine](../components/mce.md), and attaches them to the protocol's execution
    report as `MetricResult` nodes. The report keeps only the latest run: the results of a previous run are
    replaced. A protocol that was only enabled is not evaluated.

A detector or an evaluator that raises is logged and skipped. The other protocols are still handled, and the
message is reported as failed so that it can be retried.

All graph access goes through the platform's providers (`SCOPEKGProvider` to read,
`SCOPECognitionProtocolReportProvider` to write): the worker holds no queries.

## Built-in protocols

ACCORD and CONCORD are built in.

| Protocol | Detector (skills) | Evaluator (MCE metrics) |
|---|---|---|
| `accord` | `l9-accord-*` | `ioc-accord-mce-metrics`, configured by `protocols/accord_mce_config.yaml` |
| `concord` | `l9-concord-*` (`initiator`, `receiver`) | `ioc-concord-mce-metrics`, configured by `protocols/concord_mce_config.yaml` |

Both are skill-based, and handled by the same `SkillProtocolDetector`:

- a protocol is **enabled** when an agent's system prompt lists one of its skills among the available skills
  (a bullet such as `- **l9-concord-initiator**: ...`);
- it is **activated** when one of its skills was called, with `activate_skill`, or with `run_skill_script`
  against it. The calls are matched on the raw argument text, so a call whose arguments were cut short in
  storage is still seen as long as the skill name survived. A skill that was called but is not listed in any
  readable prompt still counts as enabled.

The CONCORD metrics read the initiator script's own calls. A session where only a specialist activated the
receiver is therefore recorded as activated, and every metric is skipped. A skipped metric is never stored, so it
never reads as `0`.

Each evaluator runs a list of MCE metrics, configured under `metrics: Session:` in its config file, and with the
MCE cache switched off in both directions, so that nothing is stored on the `Session`. The ACCORD config covers
convergence, phase coverage, validity of the exchanged terms, lock precision and recall, and the token overhead of
the protocol. The CONCORD config covers protocol compliance, the number of rounds, the satisfaction the protocol
records, the token cost by phase, and four LLM-judged metrics. Those four cost judge tokens for each session,
and can be commented out of the config.

## What it writes

The two report classes belong to the [Analysis ontology](../../ingestion/ontology/analysis.md).

`(:CognitionProtocolReport {id, protocolsEnabled, protocolsActivated})`

- `-[:reportsOn]->(:Session)`, always. There is one per session.

`(:CognitionProtocolExecutionReport {id, protocol, ...})`

- `-[:reportsOn]->(:Session)`, one per session and activated protocol, holding the agents and skills that had
  the protocol and the ones that used it;
- `<-[:measures]-(:MetricResult)`, one for each metric computed by the protocol's evaluator.

The Cognition Protocols views of the [UI](../../platform/ui/index.md) read these reports.

## Queues

- **Input queue**: `new_session_to_cognition_protocols`
- **Output queue(s)**: none by default
- **CLI**: `cognition-protocols-worker-cli`

The worker is a parallel branch off the pipeline, fed by the [embedding worker](embedding-worker.md)'s output
queues. It is deployed by default:

- **Helm chart:** `workers.cognition-protocols.enabled` is `true`. While it is, `new_session_to_cognition_protocols`
  is added to the embedding worker's output queues.
- **Docker Compose:** the `cognition-protocols` service is part of the stack, and the queue is in the default
  `EMBEDDING_OUTPUT_QUEUE`.

!!! warning "The image is not published by CI yet"
    The ACCORD and CONCORD metric packages are private git dependencies, so the image can only be built with an
    SSH agent that can read them (see the worker's Dockerfile), with `docker buildx bake cognition-protocols-worker`.
    It is not part of the CI image matrix yet. Until the image is available to your cluster, turn the worker off:
    `workers.cognition-protocols.enabled=false` in the chart, or stop the service in Compose and remove its queue
    from `EMBEDDING_OUTPUT_QUEUE`.

```bash
cognition-protocols-worker-cli                                  # consume new_session_to_cognition_protocols
cognition-protocols-worker-cli --run-once --input msg.json --output out.json   # one session: {"session_id": "..."}
```

## Configuration

| Env var | CLI flag | Default | Purpose |
|---|---|---|---|
| `RABBITMQ_URL` (or `RABBITMQ_*`) | `--rabbitmq-url` | none | Broker |
| `COGNITION_PROTOCOLS_INPUT_QUEUE` | `--input-queue` | `new_session_to_cognition_protocols` | Input queue |
| `COGNITION_PROTOCOLS_OUTPUT_QUEUE` | `--output-queue` | none | Output queue(s) |
| `COGNITION_PROTOCOLS_FEEDBACK_QUEUE` | `--feedback-queue` | none | Feedback queue |
| `NEO4J_URI`, with `NEO4J_AUTH` or `NEO4J_USERNAME` / `NEO4J_PASSWORD` | _(env only)_ | | Knowledge graph |
| `LLM_API_KEY` (or `OPENAI_API_KEY`), `LLM_MODEL_NAME`, `LLM_BASE_MODEL_URL` | _(env only)_ | | Judge model of the LLM-judged metrics |

`--max-sessions` limits the number of sessions processed, and `--max-inflight-messages` the number of messages
handled at once (the default, `-1`, derives it from the CPU count).

## Adding a protocol

A protocol is a **detector** (is it present, is it used?) and, if it has metrics, an **evaluator** with the same
`name`. Both are plugins: register them in code, or from another package as entry points, and nothing here
changes.

### Detector

A protocol whose agents get it as skills with a common name prefix needs no code:

```python
from cognition_protocols_worker.detectors import SkillProtocolDetector

SkillProtocolDetector("concord", "l9-concord-")
```

Any other protocol subclasses `ProtocolDetector`, and fills in a `ProtocolFinding` from the session's spans
(`SessionSpans` gives access to its agents, LLM calls and tool calls):

```python
from cognition_protocols_worker.detectors import ProtocolDetector, ProtocolFinding, SessionSpans

class MyDetector(ProtocolDetector):
    name = "my-protocol"

    def detect(self, context):
        spans = SessionSpans.of(context)          # .agents, .llms, .tools, .agent_of(span)
        found = ProtocolFinding(self.name)
        ...                                       # fill enabled / activated and the *_by_agent maps
        return found
```

Register it from a package with an entry point:

```toml
[project.entry-points.cognition_protocol_detectors]
my-protocol = "my_package.detectors:MyDetector"
```

### Evaluator

If the protocol's metrics are MCE plugins (a package registering them under `metrics_computation_engine.plugins`),
list them in an MCE worker config and use `McEvaluator`:

```python
from cognition_protocols_worker.evaluators import McEvaluator

def evaluator():
    return McEvaluator("my-protocol", "/path/to/my_protocol_mce_config.yaml")
```

```toml
[project.entry-points.cognition_protocol_evaluators]
my-protocol = "my_package.evaluator:evaluator"
```

The config is MCE's worker-config format: the metrics under `metrics: Session:`, and
`cache: {read: false, write: false}` so that nothing is stored on the `Session` (see
`protocols/accord_mce_config.yaml`). For anything that is not an MCE metric, subclass `ProtocolEvaluator` and
return an `Evaluation(passed=[...MetricResult], failed=[...])`.

An entry point may name a class, an instance, or a function returning one. Two plugins for the same protocol name
are rejected, and the clashing entry point is not loaded.

## Development

```bash
uv run --package cognition-protocols-worker --extra dev pytest workers/cognition-protocols-worker/tests -v
```
