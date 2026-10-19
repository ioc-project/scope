# cognitive-challenge-worker

Detects the cognitive challenges of the challenges taxonomy in a session and writes a `CognitiveChallengeReport`
for each: the challenge code, a `low`/`medium`/`high` confidence and a brief reasoning, attached to the session
and to the call where it happened.

It reads the session's trajectory from the knowledge graph, makes one model call over the trace and the
definitions of the configured challenges, and writes a report per detection.

Which challenges to look for is set in `cognitive_challenge_config.yaml` (see
[`backend/cognitive_challenge_config.yaml`](../../cognitive_challenge_config.yaml)); with none, nothing is
detected. The full description, the graph model and the settings are in the
[worker documentation](../../docs/workers/cognitive-challenge-worker.md).

```bash
cognitive-challenge-worker-cli --config-file cognitive_challenge_config.yaml
```

`data/challenges.json` comes from the taxonomy: `python scripts/build_challenges.py path/to/challenges-taxonomy.md`.
