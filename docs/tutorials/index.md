# SCOPE — Getting Started

<p class="eyebrow">Walkthroughs for instrumenting a system and reading what SCOPE produces</p>


## Instrument your app

The tutorial covers manual Observe SDK setup,
coding-harness workflows, distributed context propagation and verification of
traces and analysis ingestion.

<div class="button-row" markdown>
[Instrument an agent manually or with a coding harness](instrument-your-app.md){ .md-button .md-button--primary }
</div>

## Instrument an OpenClaw-style MAS

The tutorial configures the Insight Module plugin for OpenClaw-style gateways,
enables provider-SDK auto-instrumentation and verifies the exported traces. It
also shows how to adapt the approach to other agent runtimes.

[Instrument an OpenClaw-style MAS](instrument-openclaw.md){ .md-button .md-button--primary }

## Deploy SCOPE

The tutorial deploys the full stack, from the collector to the dashboard, with
either Docker Compose or Kubernetes.

<div class="button-row" markdown>
[Deploy SCOPE with Docker Compose or Kubernetes](deploy-scope.md){ .md-button .md-button--primary }
</div>

## Your first trace

The walkthrough starts the whole stack with Docker Compose, sends the trace of a small two-agent
application, and follows it to the knowledge graph in Neo4j and to the dashboard.

<div class="button-row" markdown>
[Follow your first trace from `docker compose up` to the dashboard](first-trace.md){ .md-button .md-button--primary }
</div>

## Walkthroughs

| Walkthrough | Status |
|---|---|
| [Instrument your app](instrument-your-app.md) | Available |
| [Instrument an OpenClaw-style MAS](instrument-openclaw.md) | Available |
| [Deploying SCOPE](deploy-scope.md) | Available |
| [Your first trace](first-trace.md) | Available |
| Exploring a session in the dashboard | In preparation |
| Reading cognitive-failure findings and their provenance | In preparation |
| Consuming insights through the API | In preparation |
