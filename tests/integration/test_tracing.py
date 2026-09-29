import httpx
import pytest

from conftest import DEMO_USER, GRAFANA_URL, LOKI_URL, ORDER, TEMPO_URL, eventually

TRACED_SERVICES = {"nginx", "gateway", "auth", "order", "payment"}


def fetch_spans(trace_id: str) -> dict | None:
    """Returns {span_id: span} with the owning service attached, or None while the trace is incomplete."""
    response = httpx.get(f"{TEMPO_URL}/api/traces/{trace_id}")
    if response.status_code != 200:
        return None
    spans = {}
    for batch in response.json().get("batches", []):
        attrs = {a["key"]: a["value"].get("stringValue") for a in batch["resource"]["attributes"]}
        for scope in batch.get("scopeSpans", []):
            for span in scope["spans"]:
                span["service"] = attrs.get("service.name")
                span["attrs"] = {a["key"]: next(iter(a["value"].values())) for a in span.get("attributes", [])}
                spans[span["spanId"]] = span
    # Services flush in batches; wait until every hop has arrived.
    services = {span["service"] for span in spans.values()}
    return spans if TRACED_SERVICES <= services else None


@pytest.fixture(scope="module")
def order_trace(client):
    token = client.post("/login", json=DEMO_USER).json()["access_token"]
    response = client.post("/orders", json=ORDER, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 201
    trace_id = response.headers.get("x-trace-id")
    assert trace_id and len(trace_id) == 32
    spans = eventually(lambda: fetch_spans(trace_id), timeout=60)
    assert spans, f"trace {trace_id} not complete in Tempo"
    return trace_id, spans


def server_span(spans: dict, service: str) -> dict:
    matches = [s for s in spans.values() if s["service"] == service and s["kind"] == "SPAN_KIND_SERVER"]
    assert len(matches) == 1, (service, [s["name"] for s in matches])
    return matches[0]


def parent_service(spans: dict, span: dict) -> str | None:
    parent = spans.get(span.get("parentSpanId", ""))
    return parent["service"] if parent else None


def test_single_root_is_nginx(order_trace):
    _, spans = order_trace
    roots = [s for s in spans.values() if s.get("parentSpanId", "") not in spans]
    assert len(roots) == 1
    assert roots[0]["service"] == "nginx"
    assert roots[0]["name"] == "POST /orders"


def test_call_chain_matches_architecture(order_trace):
    _, spans = order_trace
    # A server span's parent is the caller's client span (or nginx's own span for the gateway).
    assert parent_service(spans, server_span(spans, "gateway")) == "nginx"
    assert parent_service(spans, server_span(spans, "auth")) == "gateway"
    assert parent_service(spans, server_span(spans, "order")) == "gateway"
    assert parent_service(spans, server_span(spans, "payment")) == "order"


def test_datastore_spans(order_trace):
    _, spans = order_trace
    db_spans = {(s["service"], s["attrs"].get("db.system")) for s in spans.values() if s["attrs"].get("db.system")}
    assert ("auth", "redis") in db_spans
    assert ("order", "postgresql") in db_spans


def test_business_attributes(order_trace):
    _, spans = order_trace
    order_attrs = server_span(spans, "order")["attrs"]
    assert order_attrs.get("order.status") == "paid"
    assert order_attrs.get("user.id") == "alice"
    assert order_attrs.get("payment.id")
    assert server_span(spans, "payment")["attrs"].get("order.id") == order_attrs.get("order.id")


def test_logs_carry_trace_id(order_trace):
    trace_id, _ = order_trace
    expected = TRACED_SERVICES

    def services_logging_trace():
        result = httpx.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": f'{{service=~".+"}} |= "{trace_id}"', "since": "5m", "limit": 100},
        ).json()["data"]["result"]
        found = {stream["stream"]["service"] for stream in result}
        return found if expected <= found else None

    assert eventually(services_logging_trace), f"trace {trace_id} not in logs of all services"


def test_grafana_tempo_provisioned():
    # The Tempo plugin has no /health handler; prove Grafana can reach Tempo through its proxy instead.
    echo = httpx.get(f"{GRAFANA_URL}/api/datasources/proxy/uid/tempo/api/echo")
    assert echo.status_code == 200 and echo.text.strip() == "echo", echo.text
    dashboards = {d["uid"] for d in httpx.get(f"{GRAFANA_URL}/api/search", params={"type": "dash-db"}).json()}
    assert "sre-traces" in dashboards
