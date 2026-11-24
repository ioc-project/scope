# Cognition Fabric API client

## Python (`python/`)

The client package is **generated** from `../specs/oxp_openapi.yaml` with
[openapi-python-client](https://github.com/openapi-generators/openapi-python-client) and is not
committed. Only the hand-written parts are in git: `python/openapi-python-client.yaml` (generator
config), `python/scope_client/cli/` (the CLI, shipped inside the package) and `python/tests/`.

The generator version is pinned in the `client-gen` dependency group of `api/pyproject.toml`
(locked in `backend/uv.lock`) and installed with uv; bump it there.

Run these from `backend/api`:

```bash
task clients:generate      # (re)generate; skipped when the spec/config are unchanged
task clients:test-unit     # CLI unit tests
task clients:ci            # smoke tests against a running API (default http://localhost:8081)
```

CLI: `python -m scope_client.cli [--base-url URL] [--token T] <command>`; see `--help`.

### Install

The package name is `ioc-scope-client`; the Python import is `scope_client`. The generated
client requires Python 3.11 or later. Generating it in this repository uses the
workspace's Python 3.13 or later.

After a version has been published, install it from public PyPI:

```bash
python -m pip install ioc-scope-client
```

To build and install locally instead, run these commands from
`backend/api`:

```bash
uv run --locked --package ioc-scope-api --only-group client-gen \
  openapi-python-client generate \
  --path specs/oxp_openapi.yaml \
  --config clients/python/openapi-python-client.yaml \
  --meta setup --output-path clients/python --overwrite
uv build --wheel clients/python
uv venv --python 3.11 clients/python/.venv
uv pip install --python clients/python/.venv/bin/python clients/python/dist/*.whl
```

Generation is required before building: the generated package and its metadata
are not committed. The generated `python/README.md` is overwritten by generation;
keep maintained usage documentation here.

### Call the API

The client calls a running Cognition Fabric API over HTTP; it does not start the server
or connect directly to its databases. Set the base URL to the server or gateway
root, without appending `/api/v1`. The examples use `http://localhost:8000`, the
API container port; with the root Docker Compose file the API is published on
`http://localhost:8081`.

```python
from http import HTTPStatus

import httpx

from scope_client import Client
from scope_client.api.v1 import (
    get_application_names_api_v1_ui_applications_names_get as application_names,
)
from scope_client.models import ApplicationNamesResponse

with Client(
    base_url="http://localhost:8000",
    timeout=httpx.Timeout(30.0),
    raise_on_unexpected_status=True,
) as client:
    response = application_names.sync_detailed(client=client, limit=10)
    if response.status_code != HTTPStatus.OK:
        raise RuntimeError(f"API returned {response.status_code}: {response.content!r}")
    if not isinstance(response.parsed, ApplicationNamesResponse):
        raise RuntimeError("API returned an unexpected response")
    print(response.parsed.to_dict())
```

Use `AuthenticatedClient(base_url=..., token=...)` instead of `Client` when the
gateway requires bearer authentication. Each generated endpoint also provides
`sync`, `asyncio`, and `asyncio_detailed` functions.

### CLI

With the package installed in the active Python environment:

```bash
export OXP_BASE_URL=http://localhost:8000  # use :8081 with Docker Compose
# If your gateway requires authentication, set OXP_TOKEN in your environment.
python -m scope_client.cli health
python -m scope_client.cli applications --limit 10
python -m scope_client.cli sessions --limit 10
python -m scope_client.cli --help
```

Cognition-store commands (the `/api/v1/cognition-store` routes, generated from `oxp_openapi.yaml` like the rest of the client):

```bash
python -m scope_client.cli cognition-info
python -m scope_client.cli store-put --ce-id <uuid> --namespace ns --key k --type t --value '{"a": 1}'
python -m scope_client.cli store-list --namespace ns --key k
python -m scope_client.cli subscribe --namespace ns --type t --callback-url https://example.com/hook
python -m scope_client.cli subscriptions --namespace ns
python -m scope_client.cli unsubscribe <subscription-uuid>
```

The CLI defaults to `http://localhost:8006`; the Taskfile smoke tests default to
`http://localhost:8081` for Docker Compose. Set `OXP_BASE_URL` explicitly for your
deployment. Keep TLS certificate verification enabled; use `--ca-bundle` when
your gateway requires a custom CA.

### CI and releases

SCOPE's [Publish Package workflow](../../../../../.github/workflows/publish-packages.yml)
generates, tests, and builds the client. Before the first upload, configure a PyPI
Trusted Publisher for `ioc-scope-client`: repository `ioc-project/scope`, workflow
`publish-packages.yml`, environment `pypi-ioc-scope-client`. For a project that does not
exist yet, register it as a pending publisher in PyPI.

Run the workflow manually with `package: ioc-scope-client` and `publish: false`
to verify the build without uploading. Select `publish: true` to release it.
The workflow also publishes on `package-v*` tags. The client version comes
from the spec's `info.version`; bump it even for CLI-only releases.

For a new release, including a CLI-only release:

1. Update the FastAPI version in `../scope/api/__init__.py`.
2. Run `task openapi` from `backend/api` and commit the updated spec.
3. Ensure the client generation, build, and test checks pass before merging.
4. After merging, run the publish workflow with `publish: true` or use the
   package release tag. Verify the new version can be installed from PyPI.

Treat published versions as immutable. A successful job that skips an existing
artifact does not release updated code under that version.
