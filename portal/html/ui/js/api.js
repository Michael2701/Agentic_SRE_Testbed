// Every backend is reached through the portal (same origin): /faults, /experiments, /prometheus, /grafana.

export class HttpError extends Error {
  constructor(status, body) {
    super(describe(body) || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

// FastAPI errors: {"detail": "..."} or {"detail": [{loc, msg}, ...]} (validation), sometimes nested.
function describe(body) {
  if (body == null) return "";
  if (typeof body === "string") return body;
  const detail = body.detail ?? body;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => `${(d.loc || []).filter((p) => p !== "body").join(".")}: ${d.msg}`).join("\n");
  if (detail.detail) return describe(detail);
  return JSON.stringify(detail);
}

async function parse(response) {
  const text = await response.text();
  try { return JSON.parse(text); } catch { return text; }
}

export async function request(method, path, body) {
  const response = await fetch(path, {
    method, cache: "no-store",
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await parse(response);
  if (!response.ok) throw new HttpError(response.status, data);
  return data;
}

export const get = (path) => request("GET", path);
export const post = (path, body) => request("POST", path, body ?? {});
export const del = (path) => request("DELETE", path);

// ---------------------------------------------------------------- Prometheus

const PROM = "/prometheus/api/v1";

export async function promInstant(expr) {
  const data = await get(`${PROM}/query?query=${encodeURIComponent(expr)}`);
  return data.data.result.map((r) => ({ labels: r.metric, value: num(r.value[1]) }));
}

/** One series per label set: {labels, points: [[ts, value], ...]}. */
export async function promRange(expr, { seconds = 900, step = 15, end = Date.now() / 1000 } = {}) {
  const start = end - seconds;
  const data = await get(`${PROM}/query_range?query=${encodeURIComponent(expr)}&start=${start}&end=${end}&step=${step}`);
  return data.data.result.map((r) => ({ labels: r.metric, points: r.values.map(([t, v]) => [t, num(v)]) }));
}

function num(value) {
  const n = parseFloat(value);
  return Number.isFinite(n) ? n : null;
}

// ---------------------------------------------------------------- Loki and Tempo (via Grafana's datasource proxy)

export async function lokiQuery(query, { seconds = 3600, limit = 300 } = {}) {
  const end = Date.now() * 1e6;
  const start = end - seconds * 1e9;
  const data = await get(`/grafana/api/datasources/proxy/uid/loki/loki/api/v1/query_range?query=${encodeURIComponent(query)}`
    + `&start=${start}&end=${end}&limit=${limit}&direction=backward`);
  const lines = [];
  for (const stream of data.data.result) {
    for (const [ns, line] of stream.values) {
      let fields = {};
      try { fields = JSON.parse(line); } catch { fields = { msg: line }; }
      lines.push({ ts: Number(BigInt(ns) / 1000000n), service: stream.stream.service, fields, raw: line });
    }
  }
  return lines.sort((a, b) => a.ts - b.ts);
}

/** Tempo trace as a flat list of spans: {id, parent, service, name, start, end (ms), error, attrs}. */
export async function trace(traceId) {
  const data = await get(`/grafana/api/datasources/proxy/uid/tempo/api/traces/${traceId}`);
  const spans = [];
  for (const batch of data.batches || []) {
    const service = attr(batch.resource?.attributes, "service.name") || "?";
    for (const scope of batch.scopeSpans || batch.instrumentationLibrarySpans || []) {
      for (const s of scope.spans || []) {
        spans.push({
          id: s.spanId, parent: s.parentSpanId || null, service, name: s.name,
          start: Number(BigInt(s.startTimeUnixNano) / 1000n) / 1000, end: Number(BigInt(s.endTimeUnixNano) / 1000n) / 1000,
          error: s.status?.code === "STATUS_CODE_ERROR" || s.status?.code === 2,
          attrs: Object.fromEntries((s.attributes || []).map((a) => [a.key, attrValue(a.value)])),
        });
      }
    }
  }
  return spans;
}

function attr(list, key) {
  const found = (list || []).find((a) => a.key === key);
  return found ? attrValue(found.value) : null;
}

function attrValue(v) {
  if (!v) return null;
  return v.stringValue ?? v.intValue ?? v.doubleValue ?? v.boolValue ?? JSON.stringify(v);
}
