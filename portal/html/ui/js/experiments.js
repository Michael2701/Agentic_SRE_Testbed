import { del, get, post, promRange } from "./api.js";
import { Chart, Err, ago, html, pct, seconds, usePoll } from "./ui.js";
import { useState } from "../vendor/preact-htm.js";

const PHASES = ["baseline", "inject", "observe", "record", "remove", "recovery"];
// Shortened run, as in the tests and `make acceptance` (scripts/acceptance.py): offsets compressed 10x.
const SHORT = { baseline_s: 10, observe_s: 25, recovery_window_s: 5, recovery_timeout_s: 60 };
const TIME_SCALE = 0.1;
const ORDERS_P95 = 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="gateway",route="/orders",method="POST"}[30s])))';
const ORDERS_5XX = '(sum(rate(http_requests_total{job="gateway",route="/orders",method="POST",status=~"5.."}[30s])) or vector(0))'
  + ' / sum(rate(http_requests_total{job="gateway",route="/orders",method="POST"}[30s]))';

function shortOverrides(scenario) {
  const lastAt = Math.max(0, ...scenario.faults.map((f) => f.at_s || 0));
  if (!lastAt) return { durations: SHORT };
  return { time_scale: TIME_SCALE, durations: { ...SHORT, observe_s: Math.round(lastAt * TIME_SCALE) + SHORT.observe_s } };
}

const TAG_LABEL = { "same-symptom": "same symptom", "misleading-correlation": "misleading correlation", "model-breaking": "model-breaking" };
const EXPECT_LABEL = { slow: "slow", errors: "errors", auth_errors: "401s", none: "no symptom" };
const STATE_BADGE = { running: "acc", completed: "ok", failed: "bad", aborted: "warn" };

// ---------------------------------------------------------------- scenario list and launcher

function Launcher({ scenarios, blind, busy, onRun }) {
  const [short, setShort] = useState(true);
  const [rate, setRate] = useState("");
  const valid = scenarios.filter((s) => s.valid);
  const run = (scenario) => {
    const overrides = short ? shortOverrides(scenario) : {};
    if (rate) overrides.traffic = { rate: Number(rate) };
    onRun(scenario, overrides);
  };
  const random = () => run(valid[Math.floor(Math.random() * valid.length)]);
  return html`<div class="card scroll"><div class="h">Scenarios</div>
    <div class="row" style="margin-bottom:10px">
      <label class="row"><input type="checkbox" checked=${short} onChange=${(e) => setShort(e.target.checked)} />
        short run (~1.5 min, as in the tests)</label>
      <input class="in" style="width:130px" type="number" min="0.5" max="50" step="0.5" placeholder="req/s (default)"
        value=${rate} onInput=${(e) => setRate(e.target.value)} />
      <button class="btn" disabled=${busy || !valid.length} onClick=${random}>🎲 Random, blind</button>
    </div>
    ${blind ? html`<div class="notice">The list is hidden in investigation mode: names and descriptions give the cause away.
      Run a random scenario and find the cause from the telemetry; the 👁 button on the experiment reveals the answer.</div>`
    : html`<table><thead><tr><th>Scenario</th><th>Expected</th><th></th></tr></thead><tbody>
      ${scenarios.map((s) => html`<tr>
        <td><b>${s.name}</b> ${(s.tags || []).map((t) => html` <span class="badge">${TAG_LABEL[t] || t}</span>`)}
          <div class="sub">${s.description}</div>${!s.valid && html`<div class="bad sub">${JSON.stringify(s.errors)}</div>`}</td>
        <td><span class="badge">${EXPECT_LABEL[s.expect] || s.expect}</span></td>
        <td><button class="btn p sm" disabled=${busy || !s.valid} onClick=${() => run(s)}>▶ Run</button></td>
      </tr>`)}</tbody></table>`}
  </div>`;
}

// ---------------------------------------------------------------- one experiment

function Phases({ experiment }) {
  const done = new Set(experiment.phases.filter((p) => p.ended_at).map((p) => p.name));
  const now = experiment.state === "running" ? experiment.phase : null;
  const started = Object.fromEntries(experiment.phases.map((p) => [p.name, p.started_at]));
  const spec = experiment.spec?.durations || {};
  const remaining = (name) => {
    const total = { baseline: spec.baseline_s, observe: spec.observe_s }[name];
    if (!total || !started[name]) return "";
    const left = Math.max(0, total - (Date.now() - Date.parse(started[name])) / 1000);
    return ` · ${Math.round(left)}s left`;
  };
  return html`<div class="steps">${PHASES.map((p) => html`<span class=${"step " + (p === now ? "now" : done.has(p) ? "done" : "")}>
    ${p}${p === now ? remaining(p) : ""}</span>`)}</div>`;
}

function LiveChart({ experiment }) {
  const start = Date.parse(experiment.created_at) / 1000;
  const { data } = usePoll(async () => {
    // evaluated on every poll, so a running experiment's chart keeps growing
    const end = experiment.state === "running" ? Date.now() / 1000 : Date.parse(experiment.updated_at) / 1000 + 5;
    const [p95, errors] = await Promise.all([ORDERS_P95, ORDERS_5XX].map((q) =>
      promRange(q, { seconds: Math.max(30, end - start + 10), step: 5, end })));
    return { p95, errors };
  }, experiment.state === "running" ? 5000 : 0, [experiment.id, experiment.state]);
  const marks = experiment.phases.filter((p) => ["inject", "remove"].includes(p.name))
    .map((p) => ({ ts: Date.parse(p.started_at) / 1000, label: p.name, color: p.name === "inject" ? "#d29922" : "#3fb950" }));
  if (experiment.incident?.start) marks.push({ ts: Date.parse(experiment.incident.start) / 1000, label: "onset", color: "#f85149" });
  if (!data) return html`<div class="sub">loading chart…</div>`;
  return html`<div class="grid g2">
    <div><div class="sub">order p95 (Prometheus)</div><${Chart} series=${data.p95.map((s) => ({ ...s, label: "p95" }))} height=${150} format=${seconds} marks=${marks} /></div>
    <div><div class="sub">5xx share</div><${Chart} series=${data.errors.map((s) => ({ ...s, label: "5xx", color: "#f85149" }))} height=${150} format=${(v) => pct(v, 0)} marks=${marks} /></div>
  </div>`;
}

function Stats({ title, r }) {
  if (!r) return null;
  return html`<tr><td>${title}</td><td>${seconds(r.p50_s)}</td><td>${seconds(r.p95_s)}</td><td>${pct(r.error_ratio)}</td>
    <td>${pct(r.auth_error_ratio)}</td><td>${r.rps}</td></tr>`;
}

function Detail({ experiment, blind, onAbort }) {
  const [reveal, setReveal] = useState(false);
  const [error, setError] = useState(null);
  const hidden = blind && !reveal;
  const v = experiment.verdict;
  const r = experiment.results || {};
  const domains = experiment.domains?.status;
  const abort = async () => {
    try { await del(`/experiments/experiments/${experiment.id}`); onAbort(); } catch (e) { setError(e); }
  };
  return html`<div class="card"><div class="h">${experiment.id} · ${hidden ? "scenario hidden" : experiment.scenario}
      <span class="right"><span class=${"badge " + (STATE_BADGE[experiment.state] || "")}>${experiment.state}</span></span></div>
    <${Phases} experiment=${experiment} />
    <div style="margin-top:12px"><${LiveChart} experiment=${experiment} /></div>
    ${experiment.error && html`<div style="margin-top:10px"><${Err} error=${{ message: experiment.error }} /></div>`}
    ${v && html`<div class="grid g2" style="margin-top:12px">
      <div><div class="h">Verdict</div><table><tbody>
        <tr><td>expected</td><td><b>${hidden ? "hidden" : EXPECT_LABEL[v.expected] || v.expected}</b></td></tr>
        <tr><td>observed</td><td><b>${(v.observed || []).map((o) => EXPECT_LABEL[o] || o).join(", ") || "none"}</b>
          ${!hidden && html` <span class=${v.expected_symptom_seen ? "ok" : "bad"}>${v.expected_symptom_seen ? "✓" : "✗"}</span>`}</td></tr>
        <tr><td>recovered</td><td class=${v.recovered ? "ok" : "bad"}>${v.recovered ? `yes, in ${v.recovery_seconds}s` : "no"}</td></tr>
        <tr><td>domain evidence</td><td>${v.evidence_matched == null ? html`<span class="muted">not declared</span>`
          : hidden ? "hidden" : html`<span class=${v.evidence_matched ? "ok" : "bad"}>${v.evidence_matched ? "matched ✓" : "did not match ✗"}</span>`}</td></tr>
      </tbody></table></div>
      <div><div class="h">Incident (what an investigator is told)</div><table><tbody>
        <tr><td>onset</td><td>${experiment.incident?.start ? new Date(experiment.incident.start).toLocaleTimeString("en-GB") : "—"}</td></tr>
        <tr><td>end</td><td>${experiment.incident?.end ? new Date(experiment.incident.end).toLocaleTimeString("en-GB") : "—"}</td></tr>
        <tr><td>domains</td><td>${domains ? Object.entries(domains).map(([d, s]) => html`<span class=${"badge " + (s.status === "DEGRADED" ? "warn" : "ok")}
          style="margin:0 4px 4px 0">${d} ${s.status}</span>`) : "—"}</td></tr>
      </tbody></table></div>
    </div>`}
    ${(r.baseline || r.observe) && html`<div class="scroll" style="margin-top:12px"><table>
      <thead><tr><th>Phase</th><th>p50</th><th>p95</th><th>5xx</th><th>401</th><th>req/s</th></tr></thead>
      <tbody><${Stats} title="baseline" r=${r.baseline} /><${Stats} title="observe (from onset)" r=${r.observe} /><${Stats} title="observe (whole phase)" r=${r.observe_full} /></tbody>
    </table></div>`}
    <div class="row" style="margin-top:12px">
      ${experiment.state === "running" && html`<button class="btn d" onClick=${abort}>Abort</button>`}
      ${blind && html`<button class="btn" onClick=${() => setReveal(!reveal)}>👁 ${reveal ? "Hide the answer" : "Reveal the answer"}</button>`}
    </div>
    <${Err} error=${error} />
    ${!hidden && html`<div style="margin-top:12px"><div class="h">Ground truth (what was actually broken)</div>
      ${(experiment.ground_truth?.faults || []).map((f) => html`<div class="mono"><b>${f.type}</b> → ${f.target} ${JSON.stringify(f.parameters)}</div>`)}
      ${!(experiment.ground_truth?.faults || []).length && html`<div class="muted">nothing injected yet</div>`}
      <details style="margin-top:8px"><summary>Full record (JSON)</summary><pre class="json">${JSON.stringify(experiment, null, 2)}</pre></details>
    </div>`}
  </div>`;
}

// ---------------------------------------------------------------- tab

export function Experiments({ blind }) {
  const scenarios = usePoll(() => get("/experiments/scenarios"), 0);
  const list = usePoll(() => get("/experiments/experiments"), 4000);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);
  const [starting, setStarting] = useState(false);
  const experiments = [...(list.data || [])].reverse();
  const running = experiments.find((e) => e.state === "running");
  const current = experiments.find((e) => e.id === selected) || running || null;

  const onRun = async (scenario, overrides) => {
    setStarting(true); setError(null);
    try {
      const started = await post("/experiments/experiments", { scenario: scenario.name, overrides });
      setSelected(started.id);
      list.reload();
    } catch (e) { setError(e); } finally { setStarting(false); }
  };

  return html`<div class="page">
    <${Err} error=${error} />
    <div class="grid g12">
      <div class="grid" style="align-content:start">
        ${scenarios.data ? html`<${Launcher} scenarios=${scenarios.data} blind=${blind} busy=${starting || !!running} onRun=${onRun} />`
          : html`<div class="card">${scenarios.error ? html`<${Err} error=${scenarios.error} />` : "Loading…"}</div>`}
        ${running && html`<div class="sub">${running.id} is running; the next run can start after it.</div>`}
        <div class="card scroll"><div class="h">History</div>
          <table><thead><tr><th>ID</th><th>Scenario</th><th>Outcome</th><th>When</th></tr></thead><tbody>
            ${experiments.slice(0, 25).map((e) => html`<tr class="click" onClick=${() => setSelected(e.id)}>
              <td class="mono">${e.id}</td><td>${blind ? "•••" : e.scenario}</td>
              <td><span class=${"badge " + (STATE_BADGE[e.state] || "")}>${e.state}</span>
                ${e.verdict && !blind && html` <span class=${e.verdict.expected_symptom_seen && e.verdict.recovered ? "ok" : "bad"}>
                  ${e.verdict.expected_symptom_seen && e.verdict.recovered ? "✓" : "✗"}</span>`}</td>
              <td class="sub">${ago(e.created_at)}</td></tr>`)}
          </tbody></table></div>
      </div>
      <div>${current ? html`<${Detail} key=${current.id} experiment=${current} blind=${blind} onAbort=${list.reload} />`
        : html`<div class="card"><div class="notice">Run a scenario on the left. Its live phases, p95 and error charts with inject / onset / remove marks,
            the verdict and the ground truth appear here.</div></div>`}</div>
    </div>
  </div>`;
}
