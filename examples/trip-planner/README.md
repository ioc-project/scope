# Trip planner

A small multi-agent system to try SCOPE with, built with [LangGraph](https://langchain-ai.github.io/langgraph/).
A **moderator** reads a traveller's question about three fictional cities (Celestia, Verdantia and Luminos)
and hands it to the specialist that can answer it, then writes the final answer from the reply:

| Agent | Answers questions about | Tool |
|---|---|---|
| `itinerary_agent` | routes between cities and their travel times | `find_routes` |
| `activities_agent` | what to see and do in a city | `read_city_guide` |
| `concierge_agent` | train fares and discounts | `lookup_fares` |

It is instrumented with the [AGNTCY Observe SDK](https://github.com/agntcy/observe): one `@graph` that builds
the LangGraph graph (so SCOPE knows the topology of the system), one `@agent` per agent, one `@tool` per tool
and one session per question. The calls to the model are traced by the SDK's LangChain instrumentation.

## Run it

It uses any OpenAI-compatible model.

```bash
cd examples/trip-planner
export OPENAI_API_KEY=...                            # and OPENAI_BASE_URL for a gateway; MODEL sets the model (gpt-4o)
export OTLP_HTTP_ENDPOINT=http://localhost:4318      # the OTLP/HTTP collector of your SCOPE deployment
uv run trip_planner.py "What is the fastest route from Celestia to Luminos, and how many hours does the trip take?"
```

## Many sessions

[`questions.py`](questions.py) has three sets of eleven questions: each set asks for the same thing in close
wording, and the three sets ask for different things (a route, things to do, a ticket price). The last question
of each set is a trap, about Atlantis, a city the planner does not know. They are what
[the semantic analysis tutorial](../../docs/tutorials/semantic-analysis.md) groups and analyses.

```bash
uv run trip_planner.py --questions all               # one session per question, 33 in all
```

To skip the application and the model entirely, [`load_traces.py`](load_traces.py) replays 33 recorded
sessions (`traces/trip_planner_sessions.jsonl.gz`) into the collector as new sessions. It needs nothing but
Python:

```bash
python load_traces.py                                # --repeat 2 for 66 sessions
```

[`record_traces.py`](record_traces.py) records that dataset again, with your model.
