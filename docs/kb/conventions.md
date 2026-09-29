# Code conventions

- Layout: `services/<name>/{Dockerfile, requirements.txt, app/__init__.py, app/main.py, app/config.py}`;
  run with `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- Shared code is **telemetry only**, in `libs/observability` (see [observability.md](observability.md)).
  Business logic is never shared between services.
- Fault hook: `faultpoint.install(app)` **before** `instrument(app, ...)` (auth, order, payment; the Dockerfile
  also installs `/libs/faultpoint`). See [faults.md](faults.md).
- Every service: `instrument(app, "<name>")` right after creating the app; outbound HTTP through
  `observability.http.instrumented_client(...)`; wrap redis/postgres calls in `track(dep, op)`;
  log events as `logger.info("snake_case_event", extra={...})`. Never log tokens or passwords.
- Dockerfile: installs `requirements.txt` + `/libs/observability[http]` (the `[http]` extra only for
  services with outbound HTTP: gateway, order). uvicorn runs with `--no-access-log`.
  `PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10` are set because PyPI read timeouts broke builds.
- Config: a `pydantic-settings` `Settings` in `app/config.py` with in-network defaults, overridden by env in
  `docker-compose.yml` (`${VAR:-default}`); document new vars in `.env.example`.
- Clients (`httpx.AsyncClient` with `base_url`, asyncpg pool, redis) are created in FastAPI `lifespan`
  and stored on `app.state`. Every outbound HTTP call has an explicit timeout (`HTTP_TIMEOUT_SECONDS`).
- Health: `GET /health` is liveness and checks no dependencies. `GET /ready` checks dependencies
  (auth: Redis ping; order: `SELECT 1`; gateway: auth+order `/ready`). Payment has only `/health`.
- Errors: JSON `{"detail": ...}`. The gateway maps transport errors to 502 and timeouts to 504; invalid JSON → 400.
  The gateway `relay()`s downstream status and body unchanged.
- Pinned deps: fastapi 0.115.6, uvicorn 0.34.0, pydantic-settings 2.7.1, httpx 0.28.1,
  asyncpg 0.30.0, redis 5.2.1, prometheus-client 0.21.1, pytest 8.3.4.
