# Cognitive Failure Worker

Detects the cognitive failures of the challenges taxonomy (IR-1, IC-2, CC-3, ...) in a session and writes
a `CognitiveFailureReport` for each one it finds.

**Source:** [`workers/cognitive-failure-worker`](https://github.com/outshift-open/observe-and-explain-platform/tree/main/backend/workers/cognitive-failure-worker)

## How it works

For each message (`BaseQueueMessage` with a `session_id`), `CognitiveFailureWorker.handle_message`:

1. Reads the session's trajectory from the knowledge graph through the OXP API library
   (`oxp.client.dal.get_session_trajectory`): every LLM, tool and agent call in order, with the text going in
   and out. A session with no steps is skipped.
2. Makes **one** model call. It gets the trace (a block per step, with the step's id) and the definition of
   each configured failure (summary, signs and detection heuristic, from the taxonomy), and answers with
   JSON: for each detection the failure code, a confidence (`low`, `medium` or `high`), a brief reasoning,
   and the id of the step where it happened, or `null` when it only shows across the whole session.
   The answer's format is a Pydantic model (`detector.DetectionAnswer`). Its JSON schema is sent to the model as a
   `json_schema` response format, falling back to JSON mode and then to a plain call for endpoints that reject
   it, and is repeated in the prompt. Whatever comes back is validated against the model: a detection that does
   not fit (an unknown confidence, no reasoning, a failure that was not asked for) is dropped and the others
   are kept; an answer that is not the expected JSON at all is an error.
3. Writes a `CognitiveFailureReport` per detection (`oxp.client.failures`). A report is keyed by session,
   failure and call, so re-running a session overwrites the same detections; reports of an earlier run that
   are not detected again stay. A run whose answer cannot be used writes nothing.
4. Forwards the message to the output queues, if any (none by default).

Long traces are cut to about 120,000 characters, every step getting an equal share and keeping the start and
end of its text. Detection quality has not been measured yet: this is the first, simplest method, behind a
single function (`detector.detect`) so a better one can replace it.

## What it writes

`(:CognitiveFailureReport {id, name, code, confidence, reasoning, reportedAt})`

- `-[:reportsOn]->(:Session)`, always;
- `-[:atCall]->` the LLM, tool or agent call, when the detection names a step of the session.

The id comes from the session, the failure and the call, so a re-run overwrites. `CognitiveFailureReport`
is a class of the OXP ontology.

## Queues

- **Input queue**: `new_session_to_cognitive_failure`, fed by the embedding worker, next to
  `new_session_to_stateful_eval`
- **Output queue(s)**: none by default
- **CLI**: `cognitive-failure-worker-cli`

## Configuration

Which failures to look for is declared in `cognitive_failure_config.yaml` (`--config-file`, or
`COGNITIVE_FAILURE_CONFIG_PATH`; in the Helm chart the `workers.cognitive-failure.config` value):

```yaml
failures:
  - IR-1
  - CC-3
```

Codes are matched ignoring case. With no file, or an empty list, nothing is detected. A misspelt code or setting
stops the worker at startup. Every failure listed makes the one call longer (about 250 tokens each), and the
trace dominates the cost. The repo example enables the ten failures that show up in a trace on their own; the
structural ones (CR-1, CC-1, ...) are left out.

`data/failures.json` is generated from the taxonomy with `scripts/build_failures.py`.

| Env var | CLI flag | Default | Purpose |
|---|---|---|---|
| `RABBITMQ_URL` | `--rabbitmq-url` | none | Broker URL |
| `COGNITIVE_FAILURE_INPUT_QUEUE` | `--input-queue` | `new_session_to_cognitive_failure` | Input queue |
| `COGNITIVE_FAILURE_OUTPUT_QUEUE` | `--output-queue` | none | Output queue(s), comma-separated |
| `COGNITIVE_FAILURE_FEEDBACK_QUEUE` | `--feedback-queue` | none | Feedback queue |
| `COGNITIVE_FAILURE_CONFIG_PATH` | `--config-file` | none | Path to `cognitive_failure_config.yaml` |
| `MAX_INFLIGHT_MESSAGES` | `--max-inflight-messages` | `32` | Concurrent messages |
| `OPENAI_API_KEY` | `--llm-api-key` | empty | Judge model API key |
| `LLM_MODEL_NAME` | `--llm-model-name` | `gpt-4o` | Judge model |
| `LLM_BASE_MODEL_URL_MCE` | `--llm-base-model-url` | `https://api.openai.com/v1` | Judge model endpoint |
| `NEO4J_HOST` / `NEO4J_PORT` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | _(env only)_ | `localhost` / `7687` / `neo4j` / empty / `neo4j` | Knowledge graph |

## Development

```bash
uv run --package cognitive-failure-worker --extra dev pytest workers/cognitive-failure-worker/tests -v
```

The tests mock the graph and the model: no database, broker or API key is needed.
