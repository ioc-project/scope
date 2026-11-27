# failure-detection-worker

Runs the deterministic cognitive-failure detectors ([`failure-detection`](../../failure-detection)) over the
symbolic state of a session in the KG and writes one `CognitiveFailureReport` per finding. No LLM is called.

The worker is the only place where the detection library and the KG meet, and every KG access goes through the
API library:

| Who | Does what |
|---|---|
| `scope.client.neurosymbolic.get_failure_detection_input` | reads the symbolic state rows (nodes, edges) and the trajectory of the session |
| `failure_detection` (library) | rows + trajectory -> graph (`SymbolicState`, `build_delta`), rules (`Engine`), findings -> detections (`detections`) |
| `scope.client.failures.write_cognitive_failure_reports` | writes the reports |
| this worker | queue, wiring, `--failures` filter |

## How it works

For each message (`BaseQueueMessage` with a `session_id`), `FailureDetectionWorker`:

1. **Reads** the `Claim`, `Fact`, `Goal`, `Task`, `Constraint`, `AgentCapability` and `SystemPrompt` nodes the
   [symbolic-extractor-worker](../symbolic-extractor-worker) wrote, the relations between them and to the original
   nodes, and the session trajectory (with the handoffs and the final agent call). A session with no symbolic
   state is skipped.
2. **Builds** the graph the detectors read. What the KG does not hold as an element is rebuilt the way the
   extractor does: transitions between agent calls (KG handoffs + the extractor's heuristics), the claims in the
   state of each agent call, the final call, and whether a task was compared with its agent's capabilities (the
   agent has capabilities).
3. **Runs** every rule, then the end-of-session rules.
4. **Writes** a `CognitiveFailureReport` per finding, attached to the session (`reportsOn`) and to the call
   (`atCall`): the same node as the LLM-based cognitive-failure-worker, plus two fields only set here: `level`
   (`detection`: a consequence was seen; `risk`: a precondition is open, a warning) and `rule` (the rule that
   fired). The reasoning is the finding's message. `confidence` is required by the KG and ranked by the UI, but
   the rules are deterministic, so it is only derived from the level (`high` for a detection, `low` for a risk).
   Re-running a session overwrites its reports.
5. **Forwards** the message to the output queues (none by default).

Not in the KG, so not read: `providedTo` (which agent call received a constraint) and `sameClaims`. The rules
that need them do not fire (CR-1, IR-1, DC-4 CO). A task counts as compared with capabilities when its agent has
some, so a comparison that failed in the extractor looks like a gap (DC-4 CM).

## Queues

- **Input:** `new_session_to_failure_detection`, fed by the symbolic extractor worker.
- **Output:** none by default (`FAILURE_DETECTION_OUTPUT_QUEUE`).

## Running

From `backend/`:

```bash
cp workers/failure-detection-worker/.env.template workers/failure-detection-worker/.env   # Neo4j, RabbitMQ
uv sync --frozen --package failure-detection-worker --extra dev
uv run --frozen --package failure-detection-worker failure-detection-worker-cli --test    # "I'm Alive"
uv run --frozen --package failure-detection-worker failure-detection-worker-cli --run-once --input msg.json --output out.json
uv run --frozen --package failure-detection-worker failure-detection-worker-cli --failures CE-1 --failures DR-2
uv run --frozen --package failure-detection-worker python -m pytest workers/failure-detection-worker/tests
```

`msg.json` is a `BaseQueueMessage`, for example `{"session_id": "<id>", "job_id": "1", "workflow_id": "w"}`.

Docker, from `backend/`:

```bash
docker build -f workers/failure-detection-worker/deploy/docker/Dockerfile --build-context ontology=../ontology -t failure-detection-worker .
```
