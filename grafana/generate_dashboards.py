"""Source of truth for grafana/dashboards/*.json.

Edit this file, then run `python3 grafana/generate_dashboards.py` (stdlib only).
Grafana reloads the provisioned JSON within ~10s. Don't hand-edit the JSON.
Rules: one unit per panel (no dual axes), stat tiles for headlines, green/yellow/red only for thresholds.
"""

import itertools
import json
import pathlib

OUT = pathlib.Path(__file__).parent / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
APPS = 'job=~"gateway|auth|order|payment"'

_ids = itertools.count(1)

def target(expr, legend="", ds=PROM, ref="A"):
    t = {"datasource": ds, "expr": expr, "refId": ref}
    if legend:
        t["legendFormat"] = legend
    return t

def ts(title, exprs, x, y, w=12, h=8, unit="short", desc="", stack=False, soft_max=None):
    return {
        "id": next(_ids), "type": "timeseries", "title": title, "description": desc,
        "datasource": PROM, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(e, l, ref=chr(65 + i)) for i, (e, l) in enumerate(exprs)],
        "fieldConfig": {"defaults": {"unit": unit, "min": 0, "custom": {
            **({"axisSoftMax": soft_max} if soft_max else {}),
            "lineWidth": 2, "fillOpacity": 10 if stack else 0, "showPoints": "never",
            "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }

def stat(title, expr, x, y, w=6, h=4, unit="short", thresholds=None, desc="", decimals=2):
    steps = thresholds or [{"color": "text", "value": None}]
    return {
        "id": next(_ids), "type": "stat", "title": title, "description": desc,
        "datasource": PROM, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(expr)],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                     "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value",
                    "graphMode": "area", "textMode": "value"},
    }

def row(title, y):
    return {"id": next(_ids), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}

def dashboard(uid, title, panels, desc, templating=None, links=True):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["sre-testbed"],
        "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1,
        "time": {"from": "now-15m", "to": "now"}, "refresh": "10s",
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m"]},
        "templating": {"list": templating or []}, "annotations": {"list": []},
        "links": [{"type": "dashboards", "tags": ["sre-testbed"], "asDropdown": False,
                   "title": "Testbed", "includeVars": False, "keepTime": True}] if links else [],
        "panels": panels,
    }

GOOD_WARN_BAD = lambda warn, bad: [
    {"color": "green", "value": None}, {"color": "yellow", "value": warn}, {"color": "red", "value": bad}]

# ---------------------------------------------------------------- Service Overview
_ids = itertools.count(1)
p95_orders = ('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket'
              '{job="gateway",method="POST",route="/orders"}[1m])))')
overview = [
    stat("Paid orders / s", 'sum(rate(orders_total{status="paid"}[1m]))', 0, 0, unit="reqps"),
    stat("5xx error ratio", f'(sum(rate(http_requests_total{{{APPS},status=~"5.."}}[1m])) or vector(0)) / '
                            f'clamp_min(sum(rate(http_requests_total{{{APPS}}}[1m])), 1e-9)',
         6, 0, unit="percentunit", thresholds=GOOD_WARN_BAD(0.01, 0.05),
         desc="Share of inbound requests answered with 5xx across app services."),
    stat("POST /orders p95 (gateway)", p95_orders, 12, 0, unit="s", thresholds=GOOD_WARN_BAD(0.5, 2)),
    stat("Targets up", 'sum(up)', 18, 0, desc="Healthy Prometheus scrape targets (apps + exporters).", decimals=0),
    row("Traffic (RED)", 4),
    ts("Request rate by service", [(f'sum by (job) (rate(http_requests_total{{{APPS}}}[1m]))', "{{job}}")],
       0, 5, unit="reqps"),
    ts("Errors by service (5xx)", [(f'sum by (job) (rate(http_requests_total{{{APPS},status=~"5.."}}[1m])) '
                                    f'or sum by (job) (rate(http_requests_total{{{APPS}}}[1m])) * 0', "{{job}}")],
       12, 5, unit="reqps", soft_max=1),
    ts("p95 latency by service",
       [(f'histogram_quantile(0.95, sum by (job, le) (rate(http_request_duration_seconds_bucket{{{APPS}}}[1m])))', "{{job}}")],
       0, 13, unit="s"),
    ts("POST /orders latency (gateway)", [
        (p95_orders.replace("0.95", "0.50"), "p50"), (p95_orders, "p95"), (p95_orders.replace("0.95", "0.99"), "p99")],
       12, 13, unit="s", desc="End-to-end latency of the order flow as seen by the gateway."),
    ts("Responses by status (all app services)",
       [(f'sum by (status) (rate(http_requests_total{{{APPS}}}[1m]))', "{{status}}")], 0, 21, unit="reqps", stack=True),
    ts("nginx requests / s", [('rate(nginx_http_requests_total[1m])', "requests")],
       12, 21, w=6, unit="reqps", desc="From nginx stub_status via nginx-exporter."),
    ts("nginx active connections", [('nginx_connections_active', "connections")], 18, 21, w=6),
    row("Business", 29),
    ts("Logins by result", [('sum by (result) (rate(logins_total[1m]))', "{{result}}")], 0, 30, w=8, unit="reqps"),
    ts("Orders by status", [('sum by (status) (rate(orders_total[1m]))', "{{status}}")], 8, 30, w=8, unit="reqps"),
    ts("Payments by status", [('sum by (status) (rate(payments_total[1m]))', "{{status}}")], 16, 30, w=8, unit="reqps"),
    row("Process resources (app services)", 38),
    ts("CPU usage", [(f'rate(process_cpu_seconds_total{{{APPS}}}[1m])', "{{job}}")], 0, 39, unit="percentunit",
       desc="CPU seconds per second of each service process (1.0 = one core)."),
    ts("Resident memory", [(f'process_resident_memory_bytes{{{APPS}}}', "{{job}}")], 12, 39, unit="bytes"),
]

# ---------------------------------------------------------------- Dependencies
_ids = itertools.count(1)
dep = [
    row("Dependency calls (client side)", 0),
    ts("Call rate by edge", [('sum by (job, dependency) (rate(dependency_requests_total[1m]))', "{{job}} → {{dependency}}")],
       0, 1, unit="reqps"),
    ts("Failed calls by edge", [('sum by (job, dependency) (rate(dependency_requests_total{outcome!="success"}[1m])) '
                                 'or sum by (job, dependency) (rate(dependency_requests_total[1m])) * 0',
                                 "{{job}} → {{dependency}}")], 12, 1, unit="reqps", soft_max=1,
       desc="Timeouts and errors (transport errors or HTTP 5xx) as seen by the caller."),
    ts("p95 latency by edge", [('histogram_quantile(0.95, sum by (job, dependency, le) '
                                '(rate(dependency_request_duration_seconds_bucket[1m])))', "{{job}} → {{dependency}}")],
       0, 9, unit="s"),
    ts("p95 latency by operation", [('histogram_quantile(0.95, sum by (dependency, operation, le) '
                                     '(rate(dependency_request_duration_seconds_bucket[1m])))', "{{dependency}}: {{operation}}")],
       12, 9, unit="s"),
    row("PostgreSQL", 17),
    stat("Postgres up", 'pg_up', 0, 18, w=4, decimals=0, thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    ts("Connections (orders DB)", [('sum(pg_stat_database_numbackends{datname="orders"})', "backends")], 4, 18, w=10),
    ts("Transactions / s (orders DB)", [('rate(pg_stat_database_xact_commit{datname="orders"}[1m])', "commits"),
                                        ('rate(pg_stat_database_xact_rollback{datname="orders"}[1m])', "rollbacks")],
       14, 18, w=10, unit="ops"),
    row("Redis", 26),
    stat("Redis up", 'redis_up', 0, 27, w=4, decimals=0, thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    ts("Commands / s", [('rate(redis_commands_processed_total[1m])', "commands")], 4, 27, w=7, unit="ops"),
    ts("Connected clients", [('redis_connected_clients', "clients")], 11, 27, w=6),
    ts("Memory used", [('redis_memory_used_bytes', "used")], 17, 27, w=7, unit="bytes"),
]

# ---------------------------------------------------------------- Logs
_ids = itertools.count(1)
LOG_SEL = '{service=~"$service", level=~"$level"} |= "$request_id"'
logs_vars = [
    {"type": "query", "name": "service", "label": "Service", "datasource": LOKI,
     "query": {"label": "service", "type": 1, "stream": "", "refId": "LokiVariableQueryEditor-VariableQuery"},
     "definition": "label_values(service)", "includeAll": True, "multi": True, "allValue": ".+",
     "current": {"text": "All", "value": "$__all"}, "refresh": 2, "sort": 1},
    {"type": "custom", "name": "level", "label": "Level", "query": "debug,info,warning,error,critical",
     "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}},
    {"type": "textbox", "name": "request_id", "label": "Request ID", "query": "",
     "current": {"text": "", "value": ""}},
]
logs = [
    {"id": next(_ids), "type": "timeseries", "title": "Log volume by service", "datasource": LOKI,
     "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
     "targets": [target(f'sum by (service) (count_over_time({LOG_SEL} [1m]))', "{{service}}", ds=LOKI)],
     "fieldConfig": {"defaults": {"unit": "short", "min": 0, "custom": {"lineWidth": 2, "showPoints": "never",
                                                                         "drawStyle": "bars", "fillOpacity": 60,
                                                                         "stacking": {"mode": "normal"}}}, "overrides": []},
     "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                 "tooltip": {"mode": "multi", "sort": "desc"}}},
    {"id": next(_ids), "type": "timeseries", "title": "Warnings & errors by service", "datasource": LOKI,
     "description": "Empty means no warnings or errors in the selected range.",
     "gridPos": {"x": 12, "y": 0, "w": 12, "h": 7},
     "targets": [target('sum by (service, level) (count_over_time({service=~"$service", level=~"warning|error|critical"} '
                        '|= "$request_id" [1m]))', "{{service}} {{level}}", ds=LOKI)],
     "fieldConfig": {"defaults": {"unit": "short", "min": 0, "custom": {"lineWidth": 2, "showPoints": "never",
                                                                         "drawStyle": "bars", "fillOpacity": 60,
                                                                         "stacking": {"mode": "normal"}}}, "overrides": []},
     "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                 "tooltip": {"mode": "multi", "sort": "desc"}}},
    {"id": next(_ids), "type": "logs", "title": "Logs", "datasource": LOKI,
     "description": "Paste a request_id (from the X-Request-ID response header) to follow one request across services.",
     "gridPos": {"x": 0, "y": 7, "w": 24, "h": 20},
     "targets": [target(LOG_SEL, ds=LOKI)],
     "options": {"showTime": True, "showLabels": False, "showCommonLabels": False, "wrapLogMessage": True,
                 "prettifyLogMessage": False, "enableLogDetails": True, "sortOrder": "Descending", "dedupStrategy": "none"}},
]

# ---------------------------------------------------------------- Traces
_ids = itertools.count(1)
TEMPO = {"type": "tempo", "uid": "tempo"}
ORDERS_TRACEQL = '{ resource.service.name = "nginx" && name = "POST /orders" }'

def trace_table(title, query, x, y, w=12, h=10, desc=""):
    return {"id": next(_ids), "type": "table", "title": title, "description": desc, "datasource": TEMPO,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{"datasource": TEMPO, "refId": "A", "queryType": "traceql", "query": query,
                         "limit": 20, "tableType": "traces"}],
            "options": {"showHeader": True}, "fieldConfig": {"defaults": {}, "overrides": []}}

traces = [
    {"id": next(_ids), "type": "nodeGraph", "title": "Service map",
     "description": "Built by Tempo's metrics-generator from client/server span pairs. nginx emits only a server "
                    "span, so the nginx → gateway edge appears in traces but not here.",
     "datasource": TEMPO, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 12},
     "targets": [{"datasource": TEMPO, "refId": "A", "queryType": "serviceMap"}], "options": {}},
    trace_table("Recent POST /orders traces", ORDERS_TRACEQL, 0, 12,
                desc="Click a trace ID to open the full waterfall: nginx → gateway → auth/redis, order/postgres/payment."),
    trace_table("Slow POST /orders traces (> 100 ms)", ORDERS_TRACEQL[:-2] + ' && duration > 100ms }', 12, 12,
                desc="Empty is healthy: baseline POST /orders p95 is ~25 ms."),
    ts("Edge request rate (from traces)",
       [('sum by (client, server) (rate(traces_service_graph_request_total[1m]))', "{{client}} → {{server}}")],
       0, 22, unit="reqps"),
    ts("Edge p95 latency, server side (from traces)",
       [('histogram_quantile(0.95, sum by (client, server, le) '
         '(rate(traces_service_graph_request_server_seconds_bucket[1m])))', "{{client}} → {{server}}")],
       12, 22, unit="s"),
]

dashboards = {
    "service-overview.json": dashboard("sre-overview", "Service Overview", overview,
                                       "RED metrics, order-flow latency, business counters and process resources."),
    "dependencies.json": dashboard("sre-dependencies", "Dependencies", dep,
                                   "Client-side view of every dependency edge plus PostgreSQL and Redis internals."),
    "traces.json": dashboard("sre-traces", "Traces", traces,
                             "Tempo service map, recent and slow order traces, per-edge metrics from spans."),
    "logs.json": dashboard("sre-logs", "Logs", logs, "Loki logs filtered by service, level and request_id.",
                           templating=logs_vars),
}
for name, body in dashboards.items():
    (OUT / name).write_text(json.dumps(body, indent=2) + "\n")
    print("wrote", name, len(body["panels"]), "panels")
