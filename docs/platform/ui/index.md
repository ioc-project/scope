# SCOPE UI

The SCOPE dashboard is where you look at what your multi-agent systems did. It shows each application, its
agents and its sessions, and puts SCOPE's analysis next to them: what the agents are working on, how much they
cost, where they fail, and how a given session unfolded, step by step.

This page is a guide to using the dashboard. To run it on its own, against any SCOPE API, see
[Run the UI standalone](standalone.md). The last part, [Developing the UI](#developing-the-ui), is for people who
work on the dashboard itself.

## Open the dashboard

The dashboard and the API are served together, behind one address.

=== "Docker Compose"

    Open <http://localhost:8080>. The port is `SCOPE_PORT` in `.env`.

=== "Kubernetes"

    Forward the gateway to your machine, then open <http://localhost:8080>:

    ```bash
    kubectl -n scope port-forward svc/scope 8080:8080
    ```

!!! tip "The dashboard is empty at first"
    An application appears once SCOPE has ingested one of its sessions, and a session is ingested when it **ends**.
    If you just deployed SCOPE, instrument an application and run a session: see
    [Instrument your app](../../tutorials/instrument-your-app.md) and
    [Deploy SCOPE](../../tutorials/deploy-scope.md). Analyses such as semantic groups and insights fill in a little
    later, as the workers process each session.

## A tour of the dashboard

Everything is organised around **applications**: each one is a multi-agent system whose sessions SCOPE has
ingested. Start from the list of applications and open one. You can open an agent from there too, to see its own
page.

An application page has four tabs:

| Tab | Use it to |
|---|---|
| **Overview** | See what the application is made of (its agents, LLMs and tools) and its latest sessions |
| **Explain** | Understand how the application behaves across all of its sessions |
| **Monitor** | Follow metrics over time, for the whole application and for each agent |
| **Inspect** | Open individual sessions, completed or still running |

### Explain: how does the application behave?

This tab turns SCOPE's analysis into views, in two groups:

- **Agent behaviour**: what the agents are working on (the semantic groups of sessions, and their topic hierarchy),
  how they reason, and which resources they use.
- **Performance**: how they fail, the cognitive failures found in the sessions, and the Cognition protocols that
  were used.

Which of these appear depends on what is deployed: see [What you see depends on your deployment](#what-you-see-depends-on-your-deployment).

### Inspect: what happened in this session?

Open a session to follow it step by step. Completed sessions are listed, and sessions still running show a live
topology of their agents.

| Tab | What it shows |
|---|---|
| **Overview** | Cost, tokens and duration |
| **Analysis** | The session's metrics and analysis results |
| **Cognitive Observability** | The cognitive failures detected in the session, and where they happened |
| **L9 Protocols** | The Cognition protocols that ran in the session |
| **Reasoning Path** | The path of reasoning the agents followed |
| **Execution Graph** | The structure of the execution: who called whom |
| **Conversation** | The exchange between the agents |

## What you see depends on your deployment

Some views need a worker that your deployment may not run. Each of them is behind a **feature flag**, so that the
dashboard doesn't show an empty view. A flag is a setting of the deployment: changing it needs no rebuild of the
image.

| View | Feature flag | Needs | On by default |
|---|---|---|---|
| Semantic groups, topic hierarchy | `semantic_groups` | The [grouping](../../analysis/workers/grouping-worker.md) and [hierarchical grouping](../../analysis/workers/hierarchical-grouping-worker.md) workers | :material-check: |
| Insights ("how they are failing") | `insights` | The [intelligence](../../analysis/workers/intelligence-worker.md) worker | :material-check: |
| Live sessions and their topology | `live_topology` | Spans reaching SCOPE while a session runs | :material-check: |
| Stateful evaluation metrics | `stateful_eval` | The [stateful-eval](../../analysis/workers/stateful-eval-worker.md) worker | :material-check: |
| Cognitive observability | `cognitive_observability` | The [cognitive-failure](../../analysis/workers/cognitive-failure-worker.md) worker | :material-check: |
| Cognition protocols | `cognition_protocols` | The [cognition-protocols](../../analysis/workers/cognition-protocols-worker.md) worker | :material-check: |

To switch a view on or off, set its flag in your deployment:

=== "Docker Compose"

    In `.env`, set the matching variable: `FF_SEMANTIC_GROUPS`, `FF_INSIGHTS`, `FF_LIVE_TOPOLOGY`,
    `FF_STATEFUL_EVAL`, `FF_COGNITIVE_OBSERVABILITY`, or `FF_COGNITION_PROTOCOLS` to `true` or `false`. Then recreate the UI:
    `docker compose up -d ui`.

=== "Kubernetes"

    In the chart's values, under `ui.featureFlags`: `semanticGroups`, `insights`, `liveTopology`, `statefulEval`,
    `cognitiveObservability`, or `cognitionProtocols`:

    ```bash
    helm upgrade -i -n scope scope charts/scope \
      --set ui.featureFlags.cognitiveObservability=true
    ```

!!! note "Cognitive observability and Cognition protocols are off by default"
    The cognitive-failure and cognition-protocols workers are part of the default deployments, but their dashboard
    views are not switched on by default. Turn on `cognitive_observability` and `cognition_protocols` to see what
    the workers produce.

## Developing the UI

The rest of this page is for contributors to the dashboard.

### Tech stack

- [React 18](https://react.dev/) + [TypeScript](https://www.typescriptlang.org/)
- [Vite](https://vitejs.dev/): dev server and build
- [React Router](https://reactrouter.com/): routing (`src/routes`)
- [TanStack Query](https://tanstack.com/query): server-state and data fetching (`src/api`, `src/provider`)
- [MUI](https://mui.com/) + [`@open-ui-kit/core`](https://www.npmjs.com/package/@open-ui-kit/core): component
  library and design system
- [@antv/g6](https://g6.antv.antgroup.com/) and [@xyflow/react](https://reactflow.dev/): graph visualisations
  (execution graphs, topology)
- [Zustand](https://github.com/pmndrs/zustand): local UI state
- Yarn 4 (Berry) as the package manager

### Run it

To run the dashboard, with the dev server or as a container, and to see which variables its `.env` file takes, see
[Run the UI standalone](standalone.md).

### Feature flags in the code

Feature flags gate optional pages and sub-views on top of the always-on core features (applications, sessions,
metrics, execution graph). They are defined in
[`src/config/featureFlags.ts`](https://github.com/ioc-project/scope/blob/main/ui/src/config/featureFlags.ts) and
read with the [`useFeatureFlag`](https://github.com/ioc-project/scope/blob/main/ui/src/hooks/useFeatureFlag.ts)
hook.

A flag is resolved from three sources, from the lowest to the highest precedence: the built-in default, then the
`VITE_FF_*` environment variable (build or dev time), then the `window.ff*` property (runtime, injected for each
deployment, see the Docker section).

| Flag key | Env var | `window` property | Default |
|---|---|---|---|
| `semantic_groups` | `VITE_FF_SEMANTIC_GROUPS` | `ffSemanticGroups` | `true` |
| `insights` | `VITE_FF_INSIGHTS` | `ffInsights` | `true` |
| `live_topology` | `VITE_FF_LIVE_TOPOLOGY` | `ffLiveTopology` | `true` |
| `stateful_eval` | `VITE_FF_STATEFUL_EVAL` | `ffStatefulEval` | `true` |
| `impact_assessment` | `VITE_FF_IMPACT_ASSESSMENT` | `ffImpactAssessment` | `false` |
| `waste_estimation` | `VITE_FF_WASTE_ESTIMATION` | `ffWasteEstimation` | `false` |
| `neurosymbolic_eval` | `VITE_FF_NEUROSYMBOLIC_EVAL` | `ffNeurosymbolicEval` | `false` |
| `cognitive_observability` | `VITE_FF_COGNITIVE_OBSERVABILITY` | `ffCognitiveObservability` | `true` |
| `cognition_protocols` | `VITE_FF_COGNITION_PROTOCOLS` | `ffCognitionProtocols` | `true` |

`.env.sample` has an explicit `true` or `false` for every flag as a starting point. Edit it to match what your
local API instance supports.

### Scripts

| Command | Description |
|---|---|
| `yarn dev` (alias `yarn start`) | Run the app in development mode with hot reload on port `3000` |
| `yarn build` | Type-check (`tsc`), then build the production bundle into `dist/` |
| `yarn preview` | Serve the production build locally, for a quick smoke test |
| `yarn lint` | Run ESLint (`--fix`) and Prettier (`--write`) on the codebase |
| `yarn lint:check` | Run ESLint and Prettier in check-only mode (used in CI and pre-commit) |
| `yarn format` | Run Prettier (`--write`) only |

A Husky `pre-commit` hook runs `lint-staged`, which applies `yarn lint` to the staged `ts`, `tsx`, `js` and `jsx`
files.

### Project structure

```text
src/
├── api/          # Fetch functions + TanStack Query hooks per backend domain
├── assets/       # Icons, images
├── common/       # Cross-cutting constants, shared styles
├── components/   # Reusable UI + feature components (graphs, tables, cards, session views, ...)
├── config/       # Runtime config, e.g. featureFlags.ts
├── hooks/        # Shared React hooks (useFeatureFlag, ...)
├── pages/        # Route-level pages (Dashboard, Applications, ApplicationDetails, AgentDetailsPopulation, 404)
├── provider/     # App-wide providers (TanStack QueryClientProvider)
├── routes/       # Route path constants (PATHS) and the route tree (routes.tsx)
├── store/        # Zustand stores
├── theme/        # Theming
├── types/        # Shared TypeScript types
└── utils/        # Pure helper functions
```

Most pages follow a tab-based pattern: a page component (for example `pages/ApplicationDetails`) owns a set of
tabs, each backed by its own data-fetching hook and a dedicated component under `components/`. See
`components/SessionDetails` for the tabs of a session.

### How the production image works

The [`Dockerfile`](https://github.com/ioc-project/scope/blob/main/ui/Dockerfile) is a two-stage build:

1. **Build stage** (`node:20-alpine`): `yarn install && yarn build` produces the static assets in `dist/`.
2. **Runtime stage** (`nginx:alpine`): serves the static bundle. It uses nginx SSI (`ssi on;` in
   [`nginx.conf`](https://github.com/ioc-project/scope/blob/main/ui/nginx.conf)) to inject per-deployment runtime
   configuration into `index.html` when the container starts. That is how `window.restApiUrl` and each
   `window.ff*` feature flag are set without rebuilding the image.
   [`entrypoint.sh`](https://github.com/ioc-project/scope/blob/main/ui/entrypoint.sh) runs `envsubst` over
   `nginx.conf`, with the container's environment, before starting nginx. It only substitutes `VITE_API_PROXY_URL`,
   `VITE_REST_API_URL` and the `VITE_FF_*` variables.
3. Requests to `/rest` are proxied by nginx to `VITE_API_PROXY_URL`.

How to run the image, and what its `.env` file takes, is in [Run the UI standalone](standalone.md#option-b-run-it-as-a-container).
