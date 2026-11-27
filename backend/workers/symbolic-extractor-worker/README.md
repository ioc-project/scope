# symbolic-extractor-worker

Extracts the symbolic state of a session (claims, facts, goals, tasks, constraints, capabilities, system
prompts and the relations between them) and writes it to the KG, as a queue worker. It wraps the
[`symbolic-extractor`](../../symbolic-extractor) library and follows the layout of the other workers.

The worker is the only place where the extraction library and the KG meet; neither knows about the other:

| Step | Done by |
|---|---|
| 1. read the session and its trajectory (with the handoffs and the final call) as dicts, so the session must be normalised first | the API library: `scope.client.neurosymbolic.get_symbolic_extraction_input` |
| 2. parse them (`SessionData`, `SessionTrajectory`) and run the pipeline (`symbolic_extractor.pipeline.build_pipeline`) with the LLM of the environment | the library: `SymbolicExtractor.process` |
| 3. turn the result into node and edge rows (`symbolic_extractor.graph.to_graph`) | the library |
| 4. write them, replacing what was written for the session before (a new extraction can word claims differently, so their ids change) | the API library: `scope.client.neurosymbolic.write_symbolic_state` |
| 5. forward the message to the output queue | the worker |

`same_claims` is computed with the embedding model of the environment (`EMBEDDING_MODEL_NAME`, dem's
`OpenAIEmbedder` as in scope's embedding worker, API only, no `torch`); `SYMBOLIC_EXTRACTOR_NO_SAME_CLAIMS=1` skips it. The KG has no
relation for it, so it only shows in the extraction result. A failed extraction is logged and dropped, nothing is
forwarded. A write with gaps (an edge towards a KG node that is not there) is logged as a warning and the message is
still forwarded; `SymbolicExtractorWorker.extract` returns the `WriteReport`.

## Queues

- **Input:** `new_session_to_symbolic_extractor`, fed by the norm worker (its `NORM_OUTPUT_QUEUE`).
- **Output:** `new_session_to_failure_detection`, read by the failure-detection worker.

## Running

```bash
cp .env.template .env        # Neo4j, RabbitMQ, LLM
uv sync --extra dev
uv run symbolic-extractor-worker-cli --test
uv run symbolic-extractor-worker-cli --run-once --input msg.json --output out.json
uv run pytest
```

The LLM settings are those of the extractor: `OPENAI_API_KEY` (or `LLM_API_KEY`), `LLM_MODEL_NAME`,
`LLM_BASE_URL` (or `OPENAI_BASE_URL`), plus `EMBEDDING_MODEL_NAME` for the embeddings.
