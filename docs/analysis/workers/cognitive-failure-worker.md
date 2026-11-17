# Cognitive Failure Worker

Detects the cognitive failures of the challenges taxonomy (IR-1, IC-2, CC-3, ...) in a session and writes
a `CognitiveFailureReport` for each one it finds.

**Source:** [`workers/cognitive-failure-worker`](https://github.com/ioc-project/scope/tree/main/backend/workers/cognitive-failure-worker)

## How it works

For each message (`BaseQueueMessage` with a `session_id`), `CognitiveFailureWorker.handle_message`:

1. Reads the session's trajectory from the knowledge graph through the SCOPE API library
   (`scope.client.dal.get_session_trajectory`): every LLM, tool and agent call in order, with the text going in
   and out. A session with no steps is marked `incomplete`.
2. Writes the trajectory to a private temporary file (`trace_file.TraceFile`, one JSON line per step) and runs an
   agent over it (`agent.investigate`). The agent has no client for the API or the graph: it only reads that
   file, through read-only tools. Before free-form investigation it receives a bounded evidence packet:
   the captured user requests, terminal observed responses per agent invocation (including retries), and
   tool inputs/results. Failed tool results are prioritized over successful results. Long items remain on
   an explicit checklist and require additional windows; a partial read cannot clear the item.
   - `read_evidence`: a bounded window of a checklist item, with coverage and continuation offsets;
   - `list_steps`: the steps, each described as a reader would name it ("the 2nd moderator LLM call") with its
     span and id, its sizes and the start of the input;
   - `read_step`: a window of one step's input and/or output, with the offset to continue at;
   - `grep`: a case-insensitive regular expression over every step's text, with context.

   The first message gives it the definition of each configured failure (summary, signs and detection
   heuristic, from the taxonomy) and an index of the first steps of the trace. It ends by calling `submit_detections`,
   whose arguments are the Pydantic model `detector.DetectionAnswer`: for each detection the failure code, a
   confidence (`low`, `medium` or `high`), a reasoning of two to four sentences (the signs found, the evidence, where, why it meets the heuristic and
   what it led to, citing steps by description and span, never by position), and the id of the step where it happened, or
   `null` when it only shows across the whole session. The endpoint enforces that schema as the tool's
   parameters, and whatever comes back is validated against it again: a detection that does not fit (an
   unknown confidence, no reasoning, a failure that was not asked for) is dropped and the others are kept, and
   a rejected submission is sent back so the agent can correct it. The agent has at most 15 model calls; the
   last one is forced to submit. The submission must also include an explicit factual-claim comparison
   for every terminal observed response, citing delivered evidence. Missing comparisons, uncited findings,
   undelivered evidence or no usable submission make the assessment `incomplete`, even with zero findings.
   A finding must cite a delivered call/span in its reasoning; citation validity does not prove semantic correctness.
   The file is deleted when the investigation ends, whether it succeeded, failed or was cancelled. Texts
   longer than 200,000 characters are cut when written, and a worker that is killed leaves nothing for long:
   at startup the worker removes trace directories older than an hour.
3. Writes a `CognitiveFailureReport` per detection (`scope.client.failures`). A report is keyed by session,
   failure and call, so re-running a session overwrites the same detections; reports of an earlier run that
   are not detected again stay. A partial assessment can retain findings with reviewed citations; its assessment status remains incomplete.
   The worker also records assessment status and coverage on the Session, independently of findings.
   Retrieval, model and persistence exceptions mark the assessment `error` and propagate for retry.
4. Forwards the message to the output queues, if any (none by default).

Completion is a coverage safeguard, not a detector-accuracy score. It means all required *captured*
checklist text was delivered and explicit claim comparisons were submitted; it cannot establish that the
model understood the evidence, that exporter-side truncation never occurred, or that the verdict is correct.
Missing placeholders keep an assessment incomplete. Unreported capture omissions cannot be inferred.
The model endpoint must support tool calling.

The sessions-with-cognitive-observability API adds `cognitiveAssessment` with `status`, `reason`,
`requiredEvidence` and `reviewedEvidence`. Status is `unknown` for older sessions and when metadata is absent,
`in_progress` while processing, then `complete`, `incomplete` or `error`. The UI shows the qualified
“No cognitive failures detected in the reviewed captured evidence” only for complete assessments.
Older sessions require reprocessing to obtain a status. Deploy the API, worker and UI together; an older API
keeps the new UI uncertain rather than showing a clean assessment. A terminated worker may leave
`in_progress` until the queue retries. Existing failure reports are retained on reassessment, as before;
these status fields are not a versioned assessment/report history.

Changes to evidence delivery must be checked on frozen, recorded real-agent traces, using the same model,
failure definitions and trace hashes. Keep model requests, tool windows, verdicts and token usage for
inspection. Include both known failures and comparison cases with acknowledged uncertainty; labels must
not be shown to the judge. Repeated judgments of one trace measure repeatability, not independent accuracy.
A selected regression corpus does not establish general precision or recall.

## What it writes

`(:CognitiveFailureReport {id, name, code, confidence, reasoning})`

`name` is the failure's name in the taxonomy (for example "Incomplete Synthesis") and `code` its code (`CC-3`).

- `-[:reportsOn]->(:Session)`, always;
- `-[:atCall]->` the LLM, tool or agent call, when the detection names a step of the session.

The id comes from the session, the failure and the call, so a re-run overwrites. `CognitiveFailureReport`
is a class of the SCOPE ontology.

## Queues

- **Input queue**: `new_session_to_cognitive_failure`, fed by the embedding worker, next to
  `new_session_to_stateful_eval`. In the Helm chart, this queue is added to the embedding
  worker's output queues while the worker is enabled (the default)
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
stops the worker at startup. Every failure listed makes the first message longer (about 250 tokens each), and the
number of turns and what the agent reads dominate the cost. The repo example enables the ten failures that show up in a trace on their own; the
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
