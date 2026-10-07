import { promInstant, promRange } from "./api.js";
import { Chart, Dot, Err, html, pct, rate, seconds, usePoll } from "./ui.js";

const ORDERS = 'job="gateway",route="/orders",method="POST"';
const Q = {
  p95: `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{${ORDERS}}[1m])))`,
  errors: `(sum(rate(http_requests_total{${ORDERS},status=~"5.."}[1m])) or vector(0)) / sum(rate(http_requests_total{${ORDERS}}[1m]))`,
  rps: 'sum(rate(http_requests_total{job="gateway"}[1m]))',
};
const APPS = ["gateway", "auth", "order", "payment"];
const APP_Q = {
  up: 'up{job=~"gateway|auth|order|payment"}',
  rps: "sum by (job) (rate(http_requests_total[1m]))",
  p95: "histogram_quantile(0.95, sum by (job, le) (rate(http_request_duration_seconds_bucket[1m])))",
  errors: '(sum by (job) (rate(http_requests_total{status=~"5.."}[1m])) or sum by (job) (rate(http_requests_total[1m])) * 0)'
    + " / sum by (job) (rate(http_requests_total[1m]))",
};
const INFRA = [["nginx", "nginx_up"], ["postgres", "pg_up"], ["redis", "redis_up"]];
const EDGE_Q = {
  p95: "histogram_quantile(0.95, sum by (job, dependency, le) (rate(dependency_request_duration_seconds_bucket[1m])))",
  errors: '(sum by (job, dependency) (rate(dependency_requests_total{outcome!="success"}[1m]))'
    + " or sum by (job, dependency) (rate(dependency_requests_total[1m])) * 0)"
    + " / sum by (job, dependency) (rate(dependency_requests_total[1m]))",
};
// Same signals and thresholds the experiment-runner judges domains by (experiments.py DOMAIN_SIGNALS), here
// against an idle baseline of ~0: latency >= 200 ms, ratio >= 20 %, up < 1.
const P95 = (metric, sel) => `histogram_quantile(0.95, sum by (le) (rate(${metric}_bucket{${sel}}[1m])))`;
const DOMAINS = {
  application: [
    ["CPU throttled", 'max(rate(container_cpu_throttled_seconds_total{job=~"gateway|auth|order"}[1m]))', "ratio"],
    ["own 500s", '(sum(rate(http_requests_total{job=~"gateway|auth|order",status="500"}[1m])) or vector(0)) / sum(rate(http_requests_total{job=~"gateway|auth|order"}[1m]))', "ratio"],
  ],
  database: [
    ["query p95", P95("dependency_request_duration_seconds", 'job="order",dependency="postgres"'), "latency"],
    ["up", "min(pg_up)", "up"],
  ],
  redis: [
    ["command p95", P95("dependency_request_duration_seconds", 'job="auth",dependency="redis"'), "latency"],
    ["up", "min(redis_up)", "up"],
  ],
  payment: [
    ["server p95", P95("http_request_duration_seconds", 'job="payment",route="/payments"'), "latency"],
    ["client p95", P95("dependency_request_duration_seconds", 'job="order",dependency="payment"'), "latency"],
    ["5xx", '(sum(rate(http_requests_total{job="payment",status=~"5.."}[1m])) or vector(0)) / sum(rate(http_requests_total{job="payment"}[1m]))', "ratio"],
  ],
};

function degraded(kind, v) {
  if (v == null) return false;
  if (kind === "latency") return v >= 0.2;
  if (kind === "ratio") return v >= 0.2;
  return v < 1;
}

const byJob = (rows) => Object.fromEntries(rows.map((r) => [r.labels.job, r.value]));
const lastValue = (series) => {
  const pts = series[0]?.points || [];
  for (let i = pts.length - 1; i >= 0; i--) if (pts[i][1] != null) return pts[i][1];
  return null;
};

async function load() {
  const [p95, errors, rps] = await Promise.all([Q.p95, Q.errors, Q.rps].map((q) => promRange(q)));
  const [up, appRps, appP95, appErr, edgeP95, edgeErr, ...infra] = await Promise.all([
    APP_Q.up, APP_Q.rps, APP_Q.p95, APP_Q.errors, EDGE_Q.p95, EDGE_Q.errors, ...INFRA.map(([, q]) => q),
  ].map((q) => promInstant(q)));
  const domainValues = Object.fromEntries(await Promise.all(Object.entries(DOMAINS).map(async ([domain, signals]) =>
    [domain, await Promise.all(signals.map(async ([name, q, kind]) => {
      const value = (await promInstant(q))[0]?.value ?? null;
      return { name, kind, value, bad: degraded(kind, value) };
    }))])));
  const edge = (job, dep) => ({
    p95: edgeP95.find((r) => r.labels.job === job && r.labels.dependency === dep)?.value ?? null,
    errors: edgeErr.find((r) => r.labels.job === job && r.labels.dependency === dep)?.value ?? null,
  });
  return {
    kpi: { p95, errors, rps },
    apps: APPS.map((job) => ({ job, up: byJob(up)[job], rps: byJob(appRps)[job], p95: byJob(appP95)[job], errors: byJob(appErr)[job] })),
    infra: INFRA.map(([name], i) => ({ name, up: infra[i][0]?.value ?? null })),
    edges: {
      "gateway-auth": edge("gateway", "auth"), "gateway-order": edge("gateway", "order"), "auth-redis": edge("auth", "redis"),
      "order-postgres": edge("order", "postgres"), "order-payment": edge("order", "payment"),
    },
    domains: domainValues,
  };
}

function edgeState(e) {
  if (!e || (e.p95 == null && e.errors == null)) return "idle";
  if (e.errors != null && e.errors >= 0.05) return "bad";
  if (e.p95 != null && e.p95 >= 0.25) return "warn";
  return "ok";
}
const STROKE = { ok: "#3fb950", warn: "#d29922", bad: "#f85149", idle: "#3a4050" };

function ServiceMap({ edges, apps, infra }) {
  const nodeState = (name) => {
    const app = apps.find((a) => a.job === name);
    if (app) return app.up === 1 ? (app.errors >= 0.05 ? "bad" : app.p95 >= 0.5 ? "warn" : "ok") : "bad";
    const i = infra.find((x) => x.name === name);
    return i ? (i.up === 1 ? "ok" : "bad") : "idle";
  };
  const N = { nginx: [20, 85], gateway: [170, 85], auth: [340, 35], order: [340, 135], redis: [520, 20],
    postgres: [520, 95], payment: [520, 160] };
  const E = [["nginx", "gateway", null], ["gateway", "auth", "gateway-auth"], ["gateway", "order", "gateway-order"],
    ["auth", "redis", "auth-redis"], ["order", "postgres", "order-postgres"], ["order", "payment", "order-payment"]];
  const W = 96, H = 30;
  return html`<svg class="map" viewBox="0 0 640 200" role="img" aria-label="Service map">
    ${E.map(([a, b, key]) => {
      const [x1, y1] = N[a]; const [x2, y2] = N[b];
      const e = key ? edges[key] : null;
      const st = key ? edgeState(e) : "idle";
      const label = e && e.p95 != null ? seconds(e.p95) + (e.errors >= 0.01 ? ` · ${pct(e.errors, 0)} err` : "") : "";
      return html`<g>
        <line x1=${x1 + W} y1=${y1 + H / 2} x2=${x2} y2=${y2 + H / 2} stroke=${STROKE[st]} stroke-width=${st === "idle" || st === "ok" ? 2 : 3.5}>
          <title>${a} → ${b}${label ? ": p95 " + label : ""}</title></line>
        ${label && html`<text class="edge-label" text-anchor="middle" x=${x1 + W + (x2 - x1 - W) * 0.68}
          y=${y1 + H / 2 + (y2 - y1) * 0.68 - 7}>${label}</text>`}
      </g>`;
    })}
    ${Object.entries(N).map(([name, [x, y]]) => {
      const st = nodeState(name);
      return html`<g><rect x=${x} y=${y} width=${W} height=${H} rx="7" fill="#1d212a" stroke=${STROKE[st]} stroke-width="1.5"/>
        <text x=${x + W / 2} y=${y + 19} text-anchor="middle">${name}</text><title>${name}: ${st}</title></g>`;
    })}
  </svg>`;
}

function Kpi({ title, series, format, color, warnAt, note }) {
  const v = lastValue(series);
  const cls = v == null ? "muted" : warnAt != null && v >= warnAt ? "warn" : "";
  return html`<div class="card">
    <div class="h">${title}</div>
    <div class=${"big " + cls}>${v == null ? "—" : format(v)}</div>
    <div class="sub">${note}</div>
    <${Chart} series=${series.map((s) => ({ ...s, color }))} height=${46} sparkline=${true} />
  </div>`;
}

export function Overview() {
  const { data, error } = usePoll(load, 5000);
  if (!data) return html`<div class="page">${error ? html`<${Err} error=${error} />` : html`<div class="muted">Loading…</div>`}</div>`;
  const { kpi, apps, infra, edges, domains } = data;
  return html`<div class="page">
    <${Err} error=${error} />
    <div class="grid g3">
      <${Kpi} title="POST /orders · p95" series=${kpi.p95} format=${seconds} color="#58a6ff" warnAt=${0.25} note="order latency at the gateway, last 15 min" />
      <${Kpi} title="5xx errors · orders" series=${kpi.errors} format=${(v) => pct(v)} color="#f85149" warnAt=${0.05} note="share of 5xx responses" />
      <${Kpi} title="Requests per second" series=${kpi.rps} format=${rate} color="#8d95a5" note="all gateway traffic (make load generates some)" />
    </div>
    <div class="grid g21">
      <div class="card"><div class="h">Service map <span class="right">color: edge health · label: p95</span></div>
        <${ServiceMap} edges=${edges} apps=${apps} infra=${infra} /></div>
      <div class="card"><div class="h">Domains <span class="right">the signals the experiment runner judges</span></div>
        <table><tbody>${Object.entries(domains).map(([domain, signals]) => {
          const bad = signals.some((s) => s.bad);
          return html`<tr><td><${Dot} state=${bad ? "warn" : "ok"} />${domain}</td>
            <td><span class=${"badge " + (bad ? "warn" : "ok")}>${bad ? "DEGRADED" : "NORMAL"}</span></td>
            <td class="sub">${signals.map((s) => html`<div class=${s.bad ? "warn" : ""}>${s.name}: ${
              s.kind === "latency" ? seconds(s.value) : s.kind === "ratio" ? pct(s.value) : s.value ?? "—"}</div>`)}</td></tr>`;
        })}</tbody></table>
        <div class="sub" style="margin-top:8px">The proxy (nginx) is deliberately not a domain: that is what the model-breaking scenarios exploit.</div>
      </div>
    </div>
    <div class="card scroll"><div class="h">Services</div>
      <table>
        <thead><tr><th>Service</th><th>Status</th><th>Requests/s</th><th>p95</th><th>5xx</th></tr></thead>
        <tbody>
          ${apps.map((a) => html`<tr><td><${Dot} state=${a.up === 1 ? "ok" : "bad"} />${a.job}</td>
            <td>${a.up === 1 ? html`<span class="ok">up</span>` : html`<span class="bad">down</span>`}</td>
            <td>${rate(a.rps)}</td><td class=${a.p95 >= 0.5 ? "warn" : ""}>${seconds(a.p95)}</td>
            <td class=${a.errors >= 0.05 ? "bad" : ""}>${pct(a.errors)}</td></tr>`)}
          ${infra.map((i) => html`<tr><td><${Dot} state=${i.up === 1 ? "ok" : "bad"} />${i.name}</td>
            <td>${i.up === 1 ? html`<span class="ok">up</span>` : html`<span class="bad">down</span>`}</td><td colspan="3" class="sub">from its exporter</td></tr>`)}
        </tbody>
      </table>
    </div>
  </div>`;
}
