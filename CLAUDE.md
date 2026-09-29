# CLAUDE.md — knowledge base index

Index only. Open a linked file **only when the task touches that topic**; don't read everything.
Source of truth for requirements: `project.md`. User-facing docs: `README.md`.

## Rules

- **Working method (mandatory):** one milestone at a time → propose plan → **wait for approval** →
  implement only that milestone → tests → verify DoD → update docs. No later-milestone infrastructure.
  Stage 0 non-goals: LLM/agents/MCP/RAG/K8s/real auth.
- **Keep the KB current:** after every milestone, decision or discovered gotcha, update the relevant
  `docs/kb/*.md` file and this index (status table, new files). One topic per file, concise, no
  duplication of `project.md`/`README.md`.
- **Git flow:** `feature/m<N>-...` from `develop` → PR → `develop` → PR → `main`. Never commit to
  `main`/`develop` directly; a milestone is done only after the merge into `main`. See [git.md](docs/kb/git.md).
- Commit only when asked; never push (user pushes with `git ppush`).

## Milestone status

| # | Milestone | Status | Details |
|---|---|---|---|
| 1 | Working distributed application | ✅ done (`7921305`) | — |
| 2 | Observability | ✅ done (`f0fda81`) | [observability.md](docs/kb/observability.md) |
| 3 | Distributed tracing (OpenTelemetry) | ✅ implemented on `feature/m3-tracing`, awaiting PRs → develop → main | [tracing.md](docs/kb/tracing.md) |
| 4–9 | Faults → stabilization | not started | `project.md` |

## Topics

| File | Read when… |
|---|---|
| [architecture.md](docs/kb/architecture.md) | touching compose topology, ports, networks, service graph |
| [conventions.md](docs/kb/conventions.md) | writing or changing any service code (structure, config, clients, health, errors, deps) |
| [observability.md](docs/kb/observability.md) | logs, request IDs, metrics, `libs/observability`, Prometheus/Loki/Alloy/Grafana, dashboards, `make load` |
| [tracing.md](docs/kb/tracing.md) | traces, Tempo, nginx otel module, span attributes, log↔trace links, service map |
| [auth.md](docs/kb/auth.md) | login, tokens, Redis, gateway auth validation |
| [orders.md](docs/kb/orders.md) | order flow, payment simulator, PostgreSQL schema |
| [testing.md](docs/kb/testing.md) | running/writing tests, manual checks, DB/Redis inspection |
| [environment.md](docs/kb/environment.md) | host tooling issues, Docker Desktop, permission-classifier limits |
| [git.md](docs/kb/git.md) | branching/PR flow, committing, remote, history rewrites |
| [decisions.md](docs/kb/decisions.md) | "why is it done this way?" — decision log |
