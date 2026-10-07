import { get } from "./api.js";
import { Experiments } from "./experiments.js";
import { Faults } from "./faults.js";
import { Logs } from "./logs.js";
import { Overview } from "./overview.js";
import { html, usePoll, useStored } from "./ui.js";
import { render, useEffect, useState } from "../vendor/preact-htm.js";

const DASHBOARDS = [
  ["sre-overview", "Service Overview"], ["sre-dependencies", "Dependencies"], ["sre-domains", "Diagnostic domains"],
  ["sre-traces", "Traces"], ["sre-logs", "Logs"],
];

function Dashboards() {
  const [uid, setUid] = useStored("cc.dashboard", DASHBOARDS[0][0]);
  return html`<div class="page">
    <div class="row">
      ${DASHBOARDS.map(([id, title]) => html`<button class=${"tab" + (id === uid ? " on" : "")} onClick=${() => setUid(id)}>${title}</button>`)}
      <span class="spacer"></span>
      <a class="sub" href=${`/grafana/d/${uid}`} target="_blank" rel="noopener">open in full Grafana ↗</a>
    </div>
    <iframe class="frame" key=${uid} title="Grafana" src=${`/grafana/d/${uid}?kiosk&theme=dark&refresh=10s&from=now-30m&to=now`}></iframe>
  </div>`;
}

const TABS = [
  ["overview", "Overview", Overview], ["faults", "Faults", Faults], ["experiments", "Experiments", Experiments],
  ["logs", "Logs & traces", Logs], ["dashboards", "Dashboards", Dashboards],
];

function useHash() {
  const read = () => location.hash.replace(/^#\/?/, "") || "overview";
  const [hash, setHash] = useState(read);
  useEffect(() => {
    const on = () => setHash(read());
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function Status({ blind }) {
  const { data } = usePoll(async () => {
    const [faults, running] = await Promise.all([
      get("/faults/faults?state=active").catch(() => null),
      get("/experiments/experiments?state=running").catch(() => null),
    ]);
    return { faults, running };
  }, 4000);
  const faults = data?.faults;
  const running = data?.running?.[0];
  return html`
    <a class="chip" href="#faults">faults: <b class=${!blind && faults?.length ? "warn" : ""}>${blind ? "hidden" : faults ? faults.length : "?"}</b></a>
    <a class="chip" href="#experiments">experiment: <b class=${running ? "acc" : ""}>${running ? `${running.id} · ${running.phase || "…"}` : "none"}</b></a>`;
}

function App() {
  const tab = useHash();
  const [blind, setBlind] = useStored("cc.blind", false);
  const current = TABS.find(([id]) => id === tab) || TABS[0];
  const Page = current[2];
  useEffect(() => { document.title = `${current[1]} · SRE Testbed`; }, [tab]);
  return html`
    <div class="top">
      <span class="brand">SRE Testbed</span>
      <nav class="tabs">${TABS.map(([id, title]) => html`<a class=${"tab" + (id === current[0] ? " on" : "")} href=${"#" + id}>${title}</a>`)}</nav>
      <span class="spacer"></span>
      <${Status} blind=${blind} />
      <button class=${"chip click" + (blind ? " on" : "")} onClick=${() => setBlind(!blind)}
        title="Hides everything that gives the cause away: active faults, scenario names, ground truth. For practising investigations.">
        👁 investigation mode: <b>${blind ? "on" : "off"}</b></button>
    </div>
    <${Page} blind=${blind} />`;
}

render(html`<${App} />`, document.getElementById("root"));
