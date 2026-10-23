# Currency demo: a two-agent system

The smallest instrumented multi-agent system to try SCOPE with: a `rate_provider` agent looks up an
exchange rate and hands over to a `currency_assistant` agent, which converts an amount. There is no
model call, so no API key is needed.

```bash
cd examples/currency-demo
export OTLP_HTTP_ENDPOINT=http://localhost:4318   # your SCOPE deployment's OTLP/HTTP collector
uv run main.py 100
```

Each run is one session. Run it a few times with different amounts, then open the SCOPE UI once the
analysis workers have processed them.
