import { del, get, post } from "./api.js";
import { Err, ago, html, usePoll } from "./ui.js";
import { useEffect, useMemo, useState } from "../vendor/preact-htm.js";

// Descriptions for people; the catalog itself (targets, parameters, defaults, limits) comes from the injector.
export const GROUPS = [
  ["Application & dependencies", {
    payment_latency: "payment answers slowly",
    payment_error: "payment returns 5xx",
    intermittent_errors: "a share of a service's requests get 5xx",
    service_unavailable: "container stopped (connection refused)",
    dependency_timeout: "container paused: connections accepted, no answer",
    redis_unavailable: "Redis stopped",
  }],
  ["Resources", {
    cpu_saturation: "busy processes eat the container's CPU quota",
    cpu_limit: "CPU quota lowered at runtime, no restart",
    memory_pressure: "a process takes memory inside the container",
  }],
  ["Databases", {
    db_slow_query: "pg_sleep trigger on writes to orders",
    db_connection_exhaustion: "another role holds every connection slot",
    db_lock_contention: "periodic lock on the orders table",
    redis_latency: "Redis periodically stops serving clients (CLIENT PAUSE)",
  }],
  ["Network", {
    network_latency: "packet delay (tc netem), to a peer or to everything",
    packet_loss: "packet loss",
    connection_failure: "iptables: reset (reject) or silence (drop) towards a peer",
  }],
  ["Configuration & deployment", {
    incorrect_endpoint: "redeploy with a wrong dependency address",
    incorrect_timeout: "redeploy with a too-short HTTP timeout",
    bad_configuration: "redeploy with bad environment values",
    bad_deployment: "new release: crashes, 5xx, slow or harmless",
  }],
  ["Proxy (nginx)", {
    proxy_rate_limit: "too strict limit_req: queueing and 503s",
    proxy_bandwidth_limit: "limit_rate per response: slow, no errors",
  }],
];
export const DESCRIPTION = Object.assign({}, ...GROUPS.map(([, types]) => types));

// ---------------------------------------------------------------- parameter fields from the JSON schema

function fieldKind(schema) {
  if (schema.anyOf) {
    const inner = schema.anyOf.find((s) => s.type !== "null");
    return { ...fieldKind(inner), optional: true };
  }
  if (schema.enum) return { kind: "enum", options: schema.enum };
  if (schema.type === "array") return { kind: "multi", options: schema.items?.enum || [] };
  if (schema.type === "object") return { kind: "settings" };
  if (schema.type === "integer" || schema.type === "number") return { kind: "number", step: schema.type === "integer" ? 1 : "any" };
  return { kind: "text" };
}

function limits(schema) {
  const s = schema.anyOf ? schema.anyOf.find((x) => x.type !== "null") : schema;
  const parts = [];
  if (s.minimum != null || s.exclusiveMinimum != null) parts.push(`min ${s.minimum ?? `>${s.exclusiveMinimum}`}`);
  if (s.maximum != null) parts.push(`max ${s.maximum}`);
  if (s.maxLength != null) parts.push(`up to ${s.maxLength} characters`);
  return parts.join(" ");
}

function SettingsField({ value, onChange, allowed }) {
  const rows = Object.entries(value || {});
  const free = allowed.filter((k) => !(k in (value || {})));
  return html`<div>
    ${rows.map(([key, v]) => html`<div class="row" style="margin-bottom:6px">
      <code style="min-width:170px">${key}</code>
      <input class="in" style="flex:1" value=${v} onInput=${(e) => onChange({ ...value, [key]: e.target.value })} />
      <button class="btn sm" onClick=${() => { const next = { ...value }; delete next[key]; onChange(next); }}>✕</button>
    </div>`)}
    ${free.length > 0 && html`<select class="in" value="" onChange=${(e) => e.target.value && onChange({ ...value, [e.target.value]: "" })}>
      <option value="">+ add a setting…</option>${free.map((k) => html`<option value=${k}>${k}</option>`)}
    </select>`}
  </div>`;
}

function Field({ name, schema, required, value, onChange, restrict }) {
  const f = fieldKind(schema);
  const def = schema.default;
  const hint = [required ? "required" : def !== undefined && def !== null ? `default ${JSON.stringify(def)}` : "optional", limits(schema)]
    .filter(Boolean).join(" · ");
  let input;
  if (f.kind === "enum") {
    const options = restrict || f.options;
    input = html`<select class="in" value=${value ?? ""} onChange=${(e) => onChange(e.target.value || undefined)}>
      <option value="">${required ? "— choose —" : f.optional ? "— none —" : `default (${def})`}</option>
      ${options.map((o) => html`<option value=${o}>${o}</option>`)}</select>`;
  } else if (f.kind === "multi") {
    const current = value ?? def ?? [];
    input = html`<div class="checks">${f.options.map((o) => html`<label><input type="checkbox" checked=${current.includes(o)}
      onChange=${(e) => onChange(e.target.checked ? [...current, o] : current.filter((x) => x !== o))} /> ${o}</label>`)}</div>`;
  } else if (f.kind === "settings") {
    input = html`<${SettingsField} value=${value} onChange=${onChange} allowed=${restrict || []} />`;
  } else {
    input = html`<input class="in" type=${f.kind === "number" ? "number" : "text"} step=${f.step}
      placeholder=${def != null ? String(def) : ""} value=${value ?? ""} onInput=${(e) => onChange(e.target.value === "" ? undefined : e.target.value)} />`;
  }
  return html`<label>${name}</label><div>${input}<div class="hint">${hint}</div></div>`;
}

function parameters(schema, values) {
  const out = {};
  for (const [name, prop] of Object.entries(schema.properties || {})) {
    const v = values[name];
    if (v === undefined || v === "") continue;
    const f = fieldKind(prop);
    if (f.kind === "number") out[name] = Number(v);
    else if (f.kind === "settings") {
      out[name] = Object.fromEntries(Object.entries(v).map(([k, x]) => [k, x !== "" && !Number.isNaN(Number(x)) ? Number(x) : x]));
    } else out[name] = v;
  }
  return out;
}

// ---------------------------------------------------------------- inject form

function InjectForm({ catalog, onInjected }) {
  const [type, setType] = useState("payment_latency");
  const entry = catalog.find((t) => t.type === type);
  const [target, setTarget] = useState("");
  const [values, setValues] = useState({});
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setTarget(entry?.default_target || entry?.targets[0] || ""); setValues({}); setResult(null); }, [type]);
  useEffect(() => { setResult(null); }, [target]);
  if (!entry) return null;
  const schema = entry.parameters;
  const body = () => ({ type, target, parameters: parameters(schema, values) });
  const restrictFor = (name) => {
    if (type === "bad_configuration" && name === "settings") return entry.per_target?.[target];
    if (type === "incorrect_endpoint" && name === "dependency") return entry.per_target?.[target];
    if (name === "peer") return null;
    return null;
  };
  const act = async (path, ok) => {
    setBusy(true);
    try { const r = await post(path, body()); setResult({ ok: ok(r) }); onInjected?.(); }
    catch (error) { setResult({ error }); }
    finally { setBusy(false); }
  };
  return html`<div class="card"><div class="h">Inject a fault</div>
    <div class="form">
      <label>Type</label>
      <div><select class="in" value=${type} onChange=${(e) => setType(e.target.value)}>
        ${GROUPS.map(([group, types]) => html`<optgroup label=${group}>${Object.keys(types).filter((t) => catalog.some((c) => c.type === t))
          .map((t) => html`<option value=${t}>${t}</option>`)}</optgroup>`)}
      </select><div class="hint">${DESCRIPTION[type] || ""}</div></div>
      <label>Target</label>
      <div><select class="in" value=${target} onChange=${(e) => setTarget(e.target.value)}>
        ${entry.targets.map((t) => html`<option value=${t}>${t}</option>`)}</select></div>
      ${Object.entries(schema.properties || {}).map(([name, prop]) => html`<${Field} key=${type + name} name=${name} schema=${prop}
        required=${(schema.required || []).includes(name)} value=${values[name]} restrict=${restrictFor(name)}
        onChange=${(v) => setValues({ ...values, [name]: v })} />`)}
    </div>
    <div class="row" style="margin-top:12px">
      <button class="btn p" disabled=${busy} onClick=${() => act("/faults/faults", (f) => `Injected ${f.id}: ${f.type} on ${f.target}`)}>Inject</button>
      <button class="btn" disabled=${busy} onClick=${() => act("/faults/faults/validate", (r) => `Valid: ${JSON.stringify(r.parameters)}`)}>Validate</button>
    </div>
    ${result?.ok && html`<div class="good" style="margin-top:10px">${result.ok}</div>`}
    ${result?.error && html`<div style="margin-top:10px"><${Err} error=${result.error} /></div>`}
  </div>`;
}

// ---------------------------------------------------------------- lists

function FaultRow({ fault, onAction, action }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async () => {
    setBusy(true); setError(null);
    try { await del(`/faults/faults/${fault.id}`); onAction(); } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return html`<tr>
    <td><b>${fault.type}</b><div class="sub mono">${JSON.stringify(fault.parameters)}</div>
      ${fault.error && html`<div class=${fault.state === "active" ? "warn sub" : "bad sub"}>${fault.error}</div>`}
      ${error && html`<${Err} error=${error} />`}</td>
    <td>${fault.target}</td>
    <td class="sub">${ago(fault.created_at)}<div class="mono">${fault.id}</div><div>${fault.experiment_id}</div></td>
    <td>${action && html`<button class="btn d sm" disabled=${busy} onClick=${run}>${busy ? "…" : action}</button>`}</td>
  </tr>`;
}

export function Faults({ blind }) {
  const catalog = usePoll(() => get("/faults/fault-types"), 0);
  const faults = usePoll(() => get("/faults/faults"), 3000);
  const [showHistory, setShowHistory] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const all = faults.data || [];
  const active = all.filter((f) => f.state === "active");
  const cleanup = all.filter((f) => f.needs_cleanup);
  const history = useMemo(() => all.filter((f) => f.state !== "active").slice(-40).reverse(), [faults.data]);
  const removeAll = async () => {
    setBusy(true); setError(null);
    try { await del("/faults/faults"); } catch (e) { setError(e); } finally { setBusy(false); faults.reload(); }
  };
  return html`<div class="page"><div class="grid g12">
    ${catalog.data ? html`<${InjectForm} catalog=${catalog.data} onInjected=${faults.reload} />` : html`<div class="card"><${Err} error=${catalog.error} /></div>`}
    <div class="grid" style="align-content:start">
      <div class="card scroll"><div class="h">Active faults <span class="right">${blind ? "" : `${active.length}`}</span></div>
        ${blind ? html`<div class="notice">Hidden in investigation mode: find the cause from the telemetry. Turn 👁 off at the top to see the answer.</div>`
          : active.length === 0 ? html`<div class="muted">Nothing is broken.</div>`
          : html`<table><thead><tr><th>Fault</th><th>Target</th><th>When</th><th></th></tr></thead>
              <tbody>${active.map((f) => html`<${FaultRow} key=${f.id} fault=${f} onAction=${faults.reload} action="Remove" />`)}</tbody></table>`}
        <div class="row" style="margin-top:10px">
          <button class="btn d" disabled=${busy || (!blind && active.length === 0 && cleanup.length === 0)} onClick=${removeAll}>
            ${busy ? "Removing…" : "Remove all"}</button>
          <span class="sub">same as <code>make recover</code></span>
        </div>
        <${Err} error=${error || faults.error} />
      </div>
      ${!blind && cleanup.length > 0 && html`<div class="card scroll"><div class="h warn">Need cleanup</div>
        <div class="sub" style="margin-bottom:8px">Applying failed, and so did the rollback. Retry the rollback:</div>
        <table><tbody>${cleanup.map((f) => html`<${FaultRow} key=${f.id} fault=${f} onAction=${faults.reload} action="Clean up" />`)}</tbody></table></div>`}
      ${!blind && html`<div class="card scroll"><div class="h">History
          <span class="right"><a href="#" onClick=${(e) => { e.preventDefault(); setShowHistory(!showHistory); }}>${showHistory ? "hide" : `show (${history.length})`}</a></span></div>
        ${showHistory && html`<table><thead><tr><th>Fault</th><th>Target</th><th>When</th><th>Outcome</th></tr></thead><tbody>
          ${history.map((f) => html`<tr><td><b>${f.type}</b><div class="sub mono">${JSON.stringify(f.parameters)}</div>
            ${f.error && html`<div class="sub">${f.error}</div>`}</td><td>${f.target}</td><td class="sub">${ago(f.created_at)}</td>
            <td><span class=${"badge " + (f.state === "removed" ? "ok" : "bad")}>${f.state}</span></td></tr>`)}
        </tbody></table>`}</div>`}
    </div>
  </div></div>`;
}
