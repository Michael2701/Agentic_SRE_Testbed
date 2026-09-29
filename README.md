# Agentic_SRE_Testbed

A small, production-like distributed system that will later be broken in controlled,
reproducible ways to train and evaluate a multi-agent SRE system. See `project.md` for the full roadmap.

**Current state: Milestone 1 — working distributed application (happy path).**
No observability stack, tracing or fault injection yet.

## Architecture

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

| Component | Tech | Role |
|---|---|---|
| nginx | nginx 1.27 | Public entry point (`localhost:8080`), reverse proxy to gateway |
| gateway | FastAPI | Routes requests, validates tokens via auth, forwards to order |
| auth | FastAPI + Redis | Fake login, issues opaque tokens stored in Redis with TTL |
| order | FastAPI + PostgreSQL | Persists orders, charges them via payment |
| payment | FastAPI | Stateless payment simulator, always approves |
| postgres | PostgreSQL 16 | `orders` table (`db/init.sql`) |
| redis | Redis 7 | Token store (`token:<token> → user_id`) |

Only nginx publishes a port; everything else lives on the internal `backend` network.

## Requirements

- Docker with Compose v2
- GNU Make

## Usage

```bash
make up     # build and start everything, waits until all containers are healthy
make test   # run integration tests (in a container, through nginx)
make ps     # container status
make logs   # follow logs
make down   # stop the environment
```

Configuration defaults live in `docker-compose.yml`; override them by copying `.env.example` to `.env`.

Demo users (fake auth by design): `alice:alice`, `bob:bob`.

## Request flow

```text
POST /login   → gateway → auth → Redis (store token)
POST /orders  → gateway → auth /validate (Redis) → order → INSERT pending
                                                       → payment /payments
                                                       → UPDATE paid → 201
```

If the payment call fails, the order is stored as `payment_failed` and the API returns `502`.

### Example session

```bash
TOKEN=$(curl -s -XPOST localhost:8080/login -H 'content-type: application/json' \
  -d '{"username":"alice","password":"alice"}' | jq -r .access_token)

curl -s -XPOST localhost:8080/orders -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"item":"book","quantity":1,"amount_cents":1500}'

curl -s localhost:8080/orders/<order-id> -H "Authorization: Bearer $TOKEN"
```

## API

Public (via nginx, `http://localhost:8080`):

| Method | Path | Description |
|---|---|---|
| POST | `/login` | `{username, password}` → `{access_token, token_type, expires_in}`; 401 on bad credentials |
| POST | `/orders` | Bearer token; `{item, quantity, amount_cents, currency?}` → 201 order with `status: "paid"` |
| GET | `/orders/{id}` | Bearer token; returns the caller's order, 404 otherwise |
| GET | `/health` | Gateway liveness |
| GET | `/ready` | Gateway readiness (checks auth and order `/ready`) |
| GET | `/nginx-health` | Nginx liveness |

Internal:

| Service | Endpoints |
|---|---|
| auth | `POST /login`, `POST /validate` (Authorization header → `{user_id}`), `GET /health`, `GET /ready` (Redis ping) |
| order | `POST /orders`, `GET /orders/{id}` (both require `X-User-Id`), `GET /health`, `GET /ready` (`SELECT 1`) |
| payment | `POST /payments` → `{payment_id, status: "approved", ...}`, `GET /health` |

Downstream transport failures at the gateway map to `502` (unavailable) or `504` (timeout).

## Tests

`tests/integration/test_happy_path.py` runs inside the compose network against nginx and covers:
health/readiness, login success and failure, the full order happy path (create → read back from PostgreSQL),
and rejection of missing/invalid tokens.

## Repository layout

```text
nginx/nginx.conf          reverse proxy config
db/init.sql               PostgreSQL schema
services/<name>/          one FastAPI service per directory (Dockerfile, requirements.txt, app/)
tests/integration/        pytest integration suite (runs via `make test`)
```
