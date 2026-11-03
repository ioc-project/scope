# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

# Populated at build time by docker/metadata-action's bake-file output
# (see .github/workflows/reusable-docker-build-push.yml).
target "docker-metadata-action" {}

group "default" {
  targets = ["ui"]
}

group "backend" {
  targets = [
    "api",
    "norm-worker",
    "mce-worker",
    "embedding-worker",
    "grouping-worker",
    "hierarchical-grouping-worker",
    "analysis-worker",
    "anomaly-detection-worker",
    "consistency-worker",
    "normal-behaviour-worker",
    "intelligence-worker",
    "stateful-eval-worker",
    "cognitive-failure-worker",
    "cognition-protocols-worker",
    "ingestion-worker",
    "semantic-analysis-worker",
    "eval-worker",
  ]
}

target "ui" {
  inherits   = ["docker-metadata-action"]
  context    = "ui"
  dockerfile = "Dockerfile"
  platforms  = ["linux/amd64"]
}

# All backend images share the same build context (the backend/ workspace root)
# because their Dockerfiles COPY sibling packages (worker-base, dem, mce, norm,
# api, ...) alongside the component being built.
target "api" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "api/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "norm-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/norm-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "mce-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/mce-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
  # Changes on every build, so the Dockerfile's LLM price refresh is never cached.
  args = {
    MODEL_PRICES_REFRESHED_AT = timestamp()
  }
}

target "embedding-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/embedding-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "grouping-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/grouping-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "hierarchical-grouping-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/hierarchical-grouping-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "analysis-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/analysis-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "anomaly-detection-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/anomaly-detection-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "consistency-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/consistency-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "normal-behaviour-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/normal-behaviour-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "intelligence-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/intelligence-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

target "stateful-eval-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/stateful-eval-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
  # Changes on every build, so the Dockerfile's LLM price refresh is never cached.
  args = {
    MODEL_PRICES_REFRESHED_AT = timestamp()
  }
}

target "cognitive-failure-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/cognitive-failure-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}
target "cognition-protocols-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/cognition-protocols-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

# The workers of a group in one image and one process (norm + embedding, grouping + hierarchical grouping + analysis +
# intelligence, mce + stateful-eval). The images of the single workers above are still built, for Argo run-once mode.
target "ingestion-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/ingestion-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}
target "semantic-analysis-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/semantic-analysis-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}
target "eval-worker" {
  inherits   = ["docker-metadata-action"]
  context    = "backend"
  contexts   = { ontology = "ontology" }
  dockerfile = "workers/eval-worker/deploy/docker/Dockerfile"
  platforms  = ["linux/amd64"]
}
