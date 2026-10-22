# Run the UI standalone

The SCOPE dashboard is a static web application. It needs one thing from its environment: the address of a SCOPE
API. You can run it on its own, from source for development or as a container, and point it at any API that is
reachable from your browser. This page shows what to put in its `.env` file, and how to run it both ways.

!!! tip "Looking for the whole stack?"
    The Docker Compose and Kubernetes deployments run the dashboard with the API behind one gateway, and need none
    of this: see [Deploy SCOPE](../../tutorials/deploy-scope.md). This page is for running the UI on its own.

## What it needs

- **A running SCOPE API.** See [Run the API standalone](../api/standalone.md). The dashboard shows nothing
  without it.
- **For the dev server**: Node.js `lts/iron` (Node 20.x), see
  [`.nvmrc`](https://github.com/ioc-project/scope/blob/main/ui/.nvmrc) (`nvm use` selects it), and Yarn `4.5.0`,
  which Corepack provides (`corepack enable`).
- **For the container**: Docker.

The API allows requests from any origin, so a dashboard served from another address, such as
`http://localhost:3000`, can call it directly. There is nothing to configure for CORS.

## Configure it with a `.env` file

The `.env` file lives in the `ui/` directory. Start from the template:

```bash
cd ui
cp .env.sample .env
```

Every variable is prefixed `VITE_`. Which ones are read depends on how you run the UI:

| Variable | Dev server | Container | Description |
|---|:---:|:---:|---|
| `VITE_REST_API_URL` | **required** | **required** | The URL of the API's `/api/v1`, **as the browser reaches it**, for example `http://localhost:8000/api/v1` |
| `VITE_API_PROXY_URL` | not used | **required** | The URL of the API, **as the container reaches it**. The container's nginx proxies `/rest` requests to it, for example `http://host.docker.internal:8000` |
| `VITE_FF_*` | read | read | One variable per [feature flag](index.md#what-you-see-depends-on-your-deployment). Leave one blank to use its default |

!!! warning "The container does not start without `VITE_API_PROXY_URL`"
    Even if the dashboard never calls `/rest`, nginx refuses to start with an empty value (`invalid URL prefix`).
    Set it to the address of your API.

`VITE_REST_API_URL` and the `VITE_FF_*` variables are set **when the container starts**, not when the image is
built, so the same image can serve any API: change the variable and restart the container.

### The feature flags

| Variable | Default | Shows |
|---|---|---|
| `VITE_FF_SEMANTIC_GROUPS` | `true` | Semantic groups and the topic hierarchy |
| `VITE_FF_INSIGHTS` | `true` | Insights |
| `VITE_FF_LIVE_TOPOLOGY` | `true` | Live sessions and their topology |
| `VITE_FF_STATEFUL_EVAL` | `true` | Stateful evaluation metrics |
| `VITE_FF_COGNITIVE_OBSERVABILITY` | `false` | The cognitive failures of a session |
| `VITE_FF_COGNITION_PROTOCOLS` | `false` | The Cognition protocols of a session |
| `VITE_FF_IMPACT_ASSESSMENT` | `false` | Impact assessment |
| `VITE_FF_WASTE_ESTIMATION` | `false` | Waste estimation |
| `VITE_FF_NEUROSYMBOLIC_EVAL` | `false` | Neurosymbolic evaluation |

A blank value means the default above. The `.env.sample` file sets every flag explicitly, and **turns off live
topology and stateful evaluation**, which are on by default: edit it to match what your API serves. Which worker
feeds which view is explained in
[What you see depends on your deployment](index.md#what-you-see-depends-on-your-deployment).

### An example

For an API on your machine, on port 8000:

```bash
# ui/.env
VITE_REST_API_URL=http://localhost:8000/api/v1

# Container only: the API as seen from inside the container
VITE_API_PROXY_URL=http://host.docker.internal:8000

# Feature flags: blank uses the default
VITE_FF_SEMANTIC_GROUPS=true
VITE_FF_INSIGHTS=true
VITE_FF_LIVE_TOPOLOGY=true
VITE_FF_STATEFUL_EVAL=true
VITE_FF_COGNITIVE_OBSERVABILITY=true
VITE_FF_COGNITION_PROTOCOLS=true
```

## Option A: run the dev server

The dev server rebuilds the dashboard when you change the code, which suits contributors.

```bash
cd ui
yarn install
yarn dev
```

It starts on <http://localhost:3000> (the script sets the port) and opens your browser. Only `VITE_REST_API_URL`
and the feature flags are read: `VITE_API_PROXY_URL` is not used.

Variables are read when `yarn dev` starts. Restart it after editing `.env`.

## Option B: run it as a container

The container serves the production build with nginx. It is the same image that the Docker Compose stack and the
Helm chart use, configured by environment variables when it starts.

```bash
docker build -t scope-ui ui
docker run --rm -p 3000:8080 --env-file ui/.env scope-ui
```

The dashboard is on <http://localhost:3000>. The container listens on port 8080, and `-p` maps it.

Or let Compose do both. The `ui/docker-compose.yml` builds the image, reads `ui/.env` and publishes the dashboard on
port 3000:

```bash
cd ui
docker compose up -d
```

Two things to know:

- **`--env-file` does not parse the file like a shell.** Write `KEY=value` lines, with no `export` and no quotes
  around the values.
- **Two addresses for one API.** Your browser calls `VITE_REST_API_URL` directly, so it must be an address that your
  browser can reach (`localhost` is fine). `VITE_API_PROXY_URL` is used by nginx **inside** the container, where
  `localhost` is the container itself: use `host.docker.internal` for an API on your machine, or the name of the API's
  container on a shared Docker network. On Linux, add `--add-host=host.docker.internal:host-gateway` to the
  `docker run` command.

## Serve the UI and the API from the same address

The deployments of SCOPE put a gateway in front of both, so that the browser only talks to one origin. The
dashboard then uses a **relative** URL:

```bash
VITE_REST_API_URL=/api/v1
```

The gateway has to forward `/api/v1` to the API and everything else to the UI. The nginx configuration of the
Docker Compose stack,
[`deploy/docker-compose/config/nginx.conf`](https://github.com/ioc-project/scope/blob/main/deploy/docker-compose/config/nginx.conf),
is a working example. This is not needed for a standalone setup: an absolute URL is simpler.

## Check that it works

1. Open the dashboard. The list of applications is empty when the API has no data yet: that is not an error.
2. Open your browser's developer tools, on the *Network* tab, and reload. The requests to `…/api/v1/…` should
   answer with a `200`.

## If something does not work

| What you see | Why |
|---|---|
| The container exits at once, with `invalid URL prefix` in its logs | `VITE_API_PROXY_URL` is missing or empty |
| The dashboard loads, but every request fails | `VITE_REST_API_URL` is wrong, or the API cannot be reached **from your browser**. Open its URL, with `/healthz`, in a tab |
| The requests go to the UI's own address | `VITE_REST_API_URL` is empty or relative, and nothing forwards `/api/v1` to the API |
| A view is missing | Its feature flag is off, or blank with a default of `false`: see the tables above |
| A change to `.env` has no effect | Restart the dev server, or recreate the container (`docker compose up -d`, or `docker run` again) |

## Related

- [UI](index.md): what the dashboard shows, and how it is built
- [Run the API standalone](../api/standalone.md): the API behind it
- [Deploy SCOPE](../../tutorials/deploy-scope.md): the whole stack
