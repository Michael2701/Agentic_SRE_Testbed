# Project: Agentic SRE Testbed Agentic SRE Testbed — Stage 0

## Mission

Build a production-like distributed test environment for a future multi-agent SRE/self-healing system.

Stage 0 contains **NO AI agents**.

The objective is to create a small observable distributed system that can be intentionally broken in controlled and reproducible ways.

---

# Working Method

Development is divided into milestones.

**Do not implement multiple milestones at once.**

For each milestone:

1. inspect the current repository state;
2. determine what already exists;
3. propose a concrete implementation plan for this milestone;
4. list the files/components that will be created or modified;
5. wait for approval before implementation;
6. implement only the approved milestone;
7. run relevant tests;
8. verify the milestone's Definition of Done;
9. update documentation.

If the repository is empty, initialize the project structure required for Milestone 1.

Do not create infrastructure or abstractions belonging to later milestones unless they are strictly required by the current milestone.

---

# Stage 0 Roadmap

## Milestone 1 — Working Distributed Application

### Goal

Create the complete application happy path.

Architecture:

```text
                         Redis
                           ↑
                           |
Client → Nginx → Gateway → Auth Service
                  |
                  └────→ Order Service → PostgreSQL
                              |
                              ↓
                       Payment Simulator
```

Components:

- Nginx
- Gateway — FastAPI
- Auth Service — FastAPI
- Order Service — FastAPI
- Payment Simulator — FastAPI
- PostgreSQL
- Redis
- Docker Compose

Required flow:

```text
Client
  ↓
POST /login
  ↓
Gateway
  ↓
Auth
  ↓
Redis
```

Then:

```text
Client
  ↓
POST /orders
  ↓
Gateway
  ↓
Auth validation
  ↓
Order Service
  ├── PostgreSQL
  └── Payment Simulator
  ↓
successful response
```

### Definition of Done

```bash
make up
```

starts the entire environment.

The following flow works end-to-end:

```text
login
→ receive token
→ create order
→ validate authentication
→ store order in PostgreSQL
→ execute simulated payment
→ return successful order
```

Basic health endpoints exist for every service.

Integration tests verify the happy path.

No observability stack or fault injection is required yet.

---

## Milestone 2 — Observability

Add:

- structured JSON logging;
- request IDs;
- Prometheus metrics;
- Grafana;
- Loki;
- useful baseline dashboards.

Definition of Done:

Normal application behaviour can be observed through metrics and logs.

---

## Milestone 3 — Distributed Tracing

Add OpenTelemetry.

Required trace:

```text
Nginx
  ↓
Gateway
  ├── Auth
  │    └── Redis
  │
  └── Order
       ├── PostgreSQL
       └── Payment Simulator
```

Definition of Done:

A `/orders` request can be followed across the complete system.

---

## Milestone 4 — Fault Injection Foundation

Create dedicated Fault Injector.

Initial faults:

- Payment latency
- Payment HTTP 500
- Service unavailable

Fault API:

```text
POST   /faults
GET    /faults
GET    /faults/{id}
DELETE /faults/{id}
DELETE /faults
```

Every fault has:

- fault ID;
- experiment ID;
- type;
- target;
- parameters;
- timestamp;
- state.

Definition of Done:

Each injected fault creates an observable incident and can be removed.

---

## Milestone 5 — Advanced Faults

Add:

- CPU saturation;
- memory pressure;
- DB slow query;
- DB connection exhaustion;
- DB lock contention;
- Redis latency;
- Redis unavailable;
- dependency timeout;
- intermittent dependency errors.

Definition of Done:

Different root causes can produce similar external symptoms.

---

## Milestone 6 — Infrastructure Faults

Add:

- network latency;
- packet loss where practical;
- connection failure;
- incorrect dependency endpoint;
- incorrect timeout;
- bad configuration;
- simulated bad deployment.

Prefer actual container/network mechanisms where practical instead of simulating every infrastructure problem using application-level `sleep()`.

---

## Milestone 7 — Experiment Framework

Create reproducible experiments.

Experiment lifecycle:

```text
establish baseline
        ↓
start traffic
        ↓
inject fault
        ↓
observe degradation
        ↓
record experiment
        ↓
remove fault
        ↓
verify recovery
```

Every experiment stores hidden ground truth.

Example:

```json
{
  "experiment_id": "exp-143",
  "fault": {
    "type": "network_latency",
    "target": "order-to-payment",
    "parameters": {
      "latency_ms": 2000
    }
  }
}
```

Strictly separate:

```text
CONTROL PLANE
knows ground truth

        ≠

DIAGNOSTIC PLANE
sees telemetry only
```

---

## Milestone 8 — Diagnostic Challenge Scenarios

Create deliberately difficult scenarios.

### Same symptom, different causes

Example:

```text
POST /orders latency > 2 sec
```

may result from:

- Payment latency;
- DB slow query;
- connection exhaustion;
- Redis latency;
- CPU saturation;
- network latency;
- proxy problem;
- configuration error.

### Misleading correlation

Example:

```text
deployment
    ↓
5 minutes
    ↓
latency increase
```

but actual root cause is Payment Simulator.

A rollback therefore succeeds technically but does not resolve the incident.

### Model-breaking scenario

Initial diagnostic domains show:

```text
Application: NORMAL
Database:    NORMAL
Redis:       NORMAL
Payment:     NORMAL
```

but `/orders` remains slow.

Actual cause exists in another domain:

```text
Network / Proxy
```

This scenario will later test whether an AI system can conclude that its current model of the incident is insufficient.

---

## Milestone 9 — Stage 0 Stabilization

Before Stage 0 is considered complete:

- minimum 8 reliable fault scenarios;
- minimum 2 model-breaking scenarios;
- minimum 1 misleading-correlation scenario;
- reproducible experiments;
- automated E2E tests;
- automated fault tests;
- clean reset;
- complete documentation;
- Grafana dashboards;
- reliable recovery after faults.

Final developer workflow:

```bash
make up
make down
make reset
make test
make load
make fault TYPE=payment-latency
make recover
make experiment SCENARIO=payment-latency
```

---

# Final Stage 0 Definition of Done

Stage 0 is complete when:

1. The complete environment starts with one command.
2. Normal `/orders` traffic works reliably.
3. Metrics, logs and traces expose system behaviour.
4. At least 8 different fault scenarios work.
5. Multiple root causes produce similar symptoms.
6. Every experiment records hidden ground truth.
7. Faults are reversible.
8. Experiments are reproducible.
9. Telemetry is divided into diagnostic domains.
10. At least two model-breaking experiments exist.
11. At least one misleading-correlation experiment exists.
12. Integration and E2E tests pass.
13. Setup and experiment execution are documented.

---

# Explicit Non-Goals for Stage 0

Do NOT implement:

- LLM integration;
- diagnostic agents;
- planner agents;
- meta-agents;
- LangGraph;
- OpenAI Agents SDK;
- Google ADK;
- MCP;
- A2A;
- RAG;
- vector databases;
- autonomous remediation;
- Kubernetes;
- complex frontend;
- real authentication.

Those belong to later stages.

---

# First Task

Start with **Milestone 1 only**.

If the repository is empty:

1. propose the initial repository structure;
2. propose service boundaries and APIs;
3. define the minimal database schema;
4. define the Docker Compose topology;
5. define the Milestone 1 integration test;
6. present the implementation plan.

**Do not write code yet.**

Wait for approval of the Milestone 1 plan before implementation.
