# Distributed tracing (M3)

Trace of `POST /orders` (verified by `tests/integration/test_tracing.py`):
```text
nginx: POST /orders [server]  (root)
└─ gateway: POST /orders [server]
   ├─ gateway: POST /validate [client] → auth: POST /validate [server] → auth: GET [client, db.system=redis]
   └─ gateway: POST /orders [client]   → order: POST /orders [server]
        ├─ INSERT / UPDATE [client, db.system=postgresql]  (+2× SELECT = asyncpg pool reset on release; real queries)
        └─ order: POST /payments [client] → payment: POST /payments [server]
```

## Pieces
- **Tempo 2.8.2** (`tempo/config.yml`): OTLP gRPC :4317 (nginx) and HTTP :4318 (Python), API :3200, local
  storage in the container (no volume, 48h retention, as Prometheus and Loki). The metrics-generator (`service-graphs`,
  `span-metrics`) remote-writes to Prometheus (`--web.enable-remote-write-receiver`), producing
  `traces_service_graph_*` and `traces_spanmetrics_*`.
- **nginx** image `nginx:1.27-alpine-otel`, `load_module modules/ngx_otel_module.so`. `otel_trace on`
  (off for `/nginx-health` and the :8081 status server); `otel_trace_context inject` in `location /` (M10: was `propagate`; a client-supplied `traceparent` could
  pick trace IDs or turn sampling off downstream via `ParentBased`).
  Span name is `"$request_method $otel_route"`, where the `map` collapses `/orders/<id>` to
  `/orders/{order_id}` (the value must be quoted: braces). Response header `X-Trace-ID` comes from
  `$otel_trace_id`, which is also in the JSON log as `trace_id`.
- **libs/observability/tracing.py** — `setup_tracing(app, service)` is called from `instrument()`:
  - TracerProvider with `service.name`, `ParentBased(ALWAYS_ON)`, BatchSpanProcessor → OTLP/HTTP
    exporter (endpoint from env `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318`, set via compose anchor
    `x-otel-env`).
  - Library instrumentors are enabled if their extra is installed: `[http]` httpx, `[postgres]` asyncpg,
    `[redis]` redis. Services use gateway `[http]`, auth `[redis]`, order `[http,postgres]`, payment none.
  - FastAPI: `excluded_urls="/health$,/ready$,/metrics$"`, `exclude_spans=["receive","send"]`.
  - httpx client spans are renamed to `"METHOD /path"` by `OTEL_HOOKS` in `http.py` (IDs → `{id}`).
  - `annotate_span({...})` sets business attributes: `user.id` (gateway/auth/order), `order.id`,
    `order.status`, `payment.id` (order), `payment.*` + `order.id` (payment).
- **Logs:** `JsonFormatter` adds `trace_id`/`span_id` (hex) from the current span. The OTel ASGI middleware
  wraps the whole stack, so access lines are inside the server span.
- **Grafana:** Tempo datasource uid `tempo` (tracesToLogsV2 → Loki with `service.name`→`service`,
  serviceMap → Prometheus). The Loki datasource has a derived field `TraceID` (regex `"trace_id": ?"(\w+)"`)
  → Tempo. Dashboard `sre-traces`: service map, recent/slow `POST /orders` TraceQL tables, per-edge
  rate/p95 from service-graph metrics.

## Gotchas
- **The service map has no nginx → gateway edge.** ngx_otel_module emits only a server span (no client
  span), and Tempo service graphs pair client+server spans. The edge is visible in the traces themselves.
- Python spans arrive up to ~5s later than nginx's (batch processor); `/api/traces/{id}` can return a
  partial trace at first. Tests wait until all 5 services are present.
- Tempo `/api/traces/{id}` returns `{"batches": [...]}` (v1). Span IDs are base64; compare them as-is.
- The Grafana Tempo plugin has no `/api/datasources/uid/tempo/health` (404); check `.../proxy/uid/tempo/api/echo`.
- Grafana reads datasource provisioning **only at startup**: after editing `datasources.yml`, run
  `docker compose restart grafana`.
