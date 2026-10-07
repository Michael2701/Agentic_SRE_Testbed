import { lokiQuery, trace as loadTrace } from "./api.js";
import { Err, clock, html, ms } from "./ui.js";
import { useEffect, useState } from "../vendor/preact-htm.js";

const SERVICES = ["nginx", "gateway", "auth", "order", "payment"];
const RANGES = [["15 min", 900], ["1 hour", 3600], ["6 hours", 21600], ["24 hours", 86400]];
const SERVICE_COLOR = { nginx: "#58a6ff", gateway: "#bc8cff", auth: "#39c5cf", order: "#3fb950", payment: "#d29922" };
const isTraceId = (s) => /^[0-9a-f]{32}$/i.test(s);
const esc = (s) => s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');

function buildQuery({ text, service, level }) {
  let q = `{service=~"${service || SERVICES.join("|")}"}`;
  if (text) q += ` |= "${esc(text)}"`;
  if (level) q += ` | json | level="${level}"`;
  return q;
}

// Fields every line has; the rest are event fields worth showing inline.
const COMMON = new Set(["ts", "level", "service", "logger", "msg", "request_id", "trace_id", "span_id", "version"]);

function LogLine({ line, onTrace }) {
  const f = line.fields;
  const extra = Object.entries(f).filter(([k]) => !COMMON.has(k));
  const level = f.level || "info";
  return html`<div class="logline">
    <span class="muted">${clock(line.ts)}</span>
    <span style=${`color:${SERVICE_COLOR[line.service] || "inherit"}`}>${line.service}</span>
    <span class=${"lvl-" + level}>${level}</span>
    <span class="msg"><b>${f.msg}</b>${extra.map(([k, v]) => html` <span class="muted">${k}=</span>${typeof v === "object" ? JSON.stringify(v) : String(v)}`)}
      ${f.request_id && html` <span class="muted">rid=</span>${f.request_id}`}
      ${f.trace_id && html` <a href="#" onClick=${(e) => { e.preventDefault(); onTrace(f.trace_id); }}>trace ↗</a>`}</span>
  </div>`;
}

function order(spans) {
  const children = new Map();
  for (const s of spans) children.set(s.parent, [...(children.get(s.parent) || []), s]);
  const ids = new Set(spans.map((s) => s.id));
  const roots = spans.filter((s) => !s.parent || !ids.has(s.parent)).sort((a, b) => a.start - b.start);
  const out = [];
  const walk = (s, depth) => {
    out.push({ ...s, depth });
    for (const c of (children.get(s.id) || []).sort((a, b) => a.start - b.start)) walk(c, depth + 1);
  };
  roots.forEach((r) => walk(r, 0));
  return out;
}

function Waterfall({ spans }) {
  const rows = order(spans);
  const t0 = Math.min(...rows.map((s) => s.start));
  const t1 = Math.max(...rows.map((s) => s.end));
  const total = Math.max(t1 - t0, 0.001);
  const [open, setOpen] = useState(null);
  return html`<div class="wf">
    <div class="sub" style="margin-bottom:6px">${rows.length} spans · ${ms(total)} · click a row for its attributes</div>
    ${rows.map((s) => {
      const left = ((s.start - t0) / total) * 100;
      const width = Math.max(((s.end - s.start) / total) * 100, 0.3);
      const slow = s.end - s.start > total * 0.5 && s.depth > 0;
      return html`<div>
        <div class="wf-row" style="cursor:pointer" onClick=${() => setOpen(open === s.id ? null : s.id)}>
          <div class="wf-name" style=${`padding-left:${s.depth * 14}px`} title=${`${s.service}: ${s.name}`}>
            <span style=${`color:${SERVICE_COLOR[s.service] || "var(--muted)"}`}>${s.service}</span> ${s.name}</div>
          <div class="wf-track">
            <div class="wf-bar" style=${`left:${left}%;width:${width}%;background:${s.error ? "#f85149" : slow ? "#d29922" : SERVICE_COLOR[s.service] || "#8d95a5"}`}></div>
            ${left + width > 80
              ? html`<div class="wf-dur" style=${`right:${100 - left - width}%;padding-right:4px;color:#0f1115;font-weight:600`}>${ms(s.end - s.start)}</div>`
              : html`<div class="wf-dur" style=${`left:${left + width}%;padding-left:4px`}>${ms(s.end - s.start)}</div>`}
          </div>
        </div>
        ${open === s.id && html`<pre class="json">${JSON.stringify(s.attrs, null, 2)}</pre>`}
      </div>`;
    })}
  </div>`;
}

export function Logs({ initial }) {
  const [form, setForm] = useState({ text: initial || "", service: "", level: "", range: 3600 });
  const [lines, setLines] = useState(null);
  const [spans, setSpans] = useState(null);
  const [traceId, setTraceId] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const showTrace = async (id) => {
    setTraceId(id); setSpans(null);
    try { setSpans(await loadTrace(id)); } catch (e) { setError(e); }
  };
  const search = async (override) => {
    const f = { ...form, ...override };
    setBusy(true); setError(null);
    try {
      const text = f.text.trim();
      setLines(await lokiQuery(buildQuery({ ...f, text }), { seconds: f.range }));
      if (isTraceId(text)) await showTrace(text.toLowerCase());
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  useEffect(() => { search(); }, []);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return html`<div class="page">
    <div class="card">
      <form class="row" onSubmit=${(e) => { e.preventDefault(); search(); }}>
        <input class="in" style="flex:1;min-width:240px" placeholder="request id, trace id (32 hex) or text: order_payment_failed, timeout…"
          value=${form.text} onInput=${set("text")} />
        <select class="in" style="width:auto" value=${form.service} onChange=${set("service")}>
          <option value="">all app services</option>${SERVICES.map((s) => html`<option value=${s}>${s}</option>`)}</select>
        <select class="in" style="width:auto" value=${form.level} onChange=${set("level")}>
          <option value="">all levels</option><option value="error">error</option><option value="warning">warning</option><option value="info">info</option></select>
        <select class="in" style="width:auto" value=${form.range} onChange=${(e) => setForm({ ...form, range: Number(e.target.value) })}>
          ${RANGES.map(([label, s]) => html`<option value=${s}>${label}</option>`)}</select>
        <button class="btn p" type="submit" disabled=${busy}>${busy ? "Searching…" : "Search"}</button>
      </form>
      <div class="sub" style="margin-top:6px">Every response carries both ids (<code>X-Request-ID</code>, <code>X-Trace-ID</code>), and so does every
        log line. Loki query: <code>${buildQuery({ ...form, text: form.text.trim() })}</code></div>
    </div>
    <${Err} error=${error} />
    <div class="grid g2">
      <div class="card"><div class="h">Logs <span class="right">${lines ? `${lines.length} lines (max 300, newest last)` : ""}</span></div>
        ${lines == null ? html`<div class="muted">…</div>` : lines.length === 0 ? html`<div class="muted">Nothing found in this time range.</div>`
          : html`<div class="loglines">${lines.map((l) => html`<${LogLine} line=${l} onTrace=${showTrace} />`)}</div>`}
      </div>
      <div class="card"><div class="h">Trace ${traceId && html`<span class="right mono">${traceId}
          · <a href=${`/grafana/explore?left=${encodeURIComponent(JSON.stringify({ datasource: "tempo", queries: [{ refId: "A", queryType: "traceql", query: traceId }] }))}`}
            target="_blank" rel="noopener">in Grafana ↗</a></span>`}</div>
        ${!traceId ? html`<div class="notice">Click “trace ↗” on a log line, or search for a trace id.</div>`
          : spans == null ? html`<div class="muted">loading…</div>`
          : spans.length === 0 ? html`<div class="muted">Trace not found (Tempo stores traces a few seconds late).</div>`
          : html`<${Waterfall} spans=${spans} />`}
      </div>
    </div>
  </div>`;
}
