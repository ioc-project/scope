# SCOPE Helm Chart

This document describes how to deploy the `scope` umbrella chart.

## Quick start

Build the chart dependencies locally before installing:

```bash
helm dependency build charts/scope
helm upgrade -i -n scope scope charts/scope --create-namespace
```

By default, the chart enables an internal SCOPE gateway service (`scope-gateway`) that routes:

- `/` to UI
- `/gql`, `/api/v1`, and `/api/v1alpha` to API

This enables a single local port-forward workflow:

```bash
kubectl -n scope port-forward svc/scope-gateway 8080:8080
```

Then open `http://localhost:8080`.

For ingress-based environments, keep this gateway pattern and expose only the gateway
via ingress to preserve same-origin browser calls.

## Deployment details for SCOPE with Helm

### 1. Prerequisites

- A Kubernetes cluster (for example Kind).
- Helm 3.x.
- `kubectl` configured for your target cluster.

### 2. Create Namespace

```bash
kubectl create namespace scope
```

### 3. Create Image Pull Secret (GHCR)

Create a Docker registry secret in the same namespace where you install the chart.

```bash
kubectl -n scope create secret docker-registry ghcr-pull-secret \
  --docker-server=ghcr.io \
  --docker-username=<github-username> \
  --docker-password=<github-token> \
  --docker-email=<email>
```

Use a GitHub token with at least `read:packages` scope.

### 4. Configure Global Pull Secret

In `values.yaml`, set:

```yaml
global:
  imagePullSecrets:
    - name: ghcr-pull-secret
```

This is used by workers and also by API/UI by default.

### 5. Existing Secrets Pattern (Summary)

Most components support the same pattern:

- If an `existingSecret`-style field is empty, the chart creates a secret from inline values.
- If that field is set, the chart reads credentials from your pre-created Kubernetes Secret.

This pattern is used across several components, including LLM, ClickHouse, RabbitMQ, and Neo4j.

RabbitMQ is the exception where the split configuration path needs a real password. For local testing, the chart ships with a non-empty placeholder, but for any shared or production deployment you should set `rabbitmq.auth.existingSecret` and store credentials in Kubernetes instead of relying on inline values.

General example:

```bash
kubectl -n scope create secret generic <your-secret-name> \
  --from-literal=<username-key>=<username> \
  --from-literal=<password-key>=<password>
```

Then point the component values to that secret name/key mapping.

For Neo4j specifically, the existing auth secret commonly uses one key with `username/password` format:

```bash
kubectl -n scope create secret generic scope-neo4j-secret \
  --from-literal=NEO4J_AUTH='neo4j/<password>'
```

Important for local/simple installs:

- Neo4j data is persistent. If a prior install already initialized a different password,
  reinstalling the chart may keep the old DB password via existing PVC/secret state.
- If workers fail with `Neo.ClientError.Security.Unauthorized`, either:
  1) provide an explicit existing auth secret that matches the running Neo4j DB password, or
  2) reset the local Neo4j data/secret before reinstalling.

### 6. Cognitive Failure Worker

The `cognitive-failure` worker detects cognitive failures of the challenges taxonomy in each session. It is
**enabled by default**, and makes a judge-model call (an agent that reads the trace) for every session. It uses
the same LLM secret as the other workers, so the model must support tool calling. To skip that cost, turn it off:

```bash
helm upgrade -i -n scope scope charts/scope \
  --set workers.cognitive-failure.enabled=false
```

While it is enabled, its input queue, `new_session_to_cognitive_failure`, is added to the embedding worker's
output queues. The failures to look for are listed in `workers.cognitive-failure.config.failures`; only the ones listed
run, and each one makes the judge prompt longer.

### 7. Cognition Protocols Worker

The `cognition-protocols` worker detects which Cognition protocols (ACCORD, CONCORD) a session used, and evaluates
the ones it used. It is **enabled by default**. Its image is not published by CI yet, because the packages with the
protocol metrics are private git dependencies, so the image has to be built with an SSH agent that can read them
(see its Dockerfile) and made available to the cluster. Until then, its pod cannot pull the image: turn the worker
off.

```bash
helm upgrade -i -n scope scope charts/scope \
  --set workers.cognition-protocols.enabled=false
```

While it is enabled, its input queue, `new_session_to_cognition_protocols`, is added to the embedding worker's
output queues. The LLM-judged metrics use the same LLM secret as the other workers.

### 8. Install / Upgrade

From a local checkout of this repository:

```bash
helm dependency build charts/scope
helm upgrade -i -n scope scope charts/scope
```

### 9. Verify

```bash
kubectl get pods -n scope
kubectl get svc -n scope
```
