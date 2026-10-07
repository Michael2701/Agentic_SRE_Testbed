import { html, useEffect, useRef, useState, useCallback } from "../vendor/preact-htm.js";
import uPlot from "../vendor/uPlot.esm.js";

export { html };

// ---------------------------------------------------------------- formatting

export function seconds(s) {
  if (s == null || Number.isNaN(s)) return "—";
  if (s < 0.001) return `${(s * 1e6).toFixed(0)} µs`;
  if (s < 1) return `${(s * 1000).toFixed(s < 0.01 ? 1 : 0)} ms`;
  return `${s.toFixed(2)} s`;
}
export const ms = (v) => (v == null ? "—" : seconds(v / 1000));
export const pct = (r, digits = 1) => (r == null || Number.isNaN(r) ? "—" : `${(r * 100).toFixed(digits)} %`);
export const rate = (r) => (r == null ? "—" : r.toFixed(r < 10 ? 1 : 0));
export function clock(ts) {
  const d = new Date(ts);
  return d.toLocaleTimeString("en-GB", { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}
export function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleString("en-GB");
}

// ---------------------------------------------------------------- hooks

/** Calls `load` now and every `ms` while mounted; returns {data, error, reload}. */
export function usePoll(load, ms, deps = []) {
  const [state, setState] = useState({ data: null, error: null });
  const alive = useRef(true);
  const run = useCallback(async () => {
    try {
      const data = await load();
      if (alive.current) setState({ data, error: null });
    } catch (error) {
      if (alive.current) setState((s) => ({ data: s.data, error }));
    }
  }, deps);
  useEffect(() => {
    alive.current = true;
    run();
    const timer = ms ? setInterval(run, ms) : null;
    return () => { alive.current = false; if (timer) clearInterval(timer); };
  }, [run, ms]);
  return { ...state, reload: run };
}

/** localStorage-backed state (per-viewer convenience); falls back to memory when storage is unavailable. */
export function useStored(key, initial) {
  const [value, setValue] = useState(() => {
    try { const raw = localStorage.getItem(key); return raw == null ? initial : JSON.parse(raw); } catch { return initial; }
  });
  const set = useCallback((v) => {
    setValue(v);
    try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* private mode */ }
  }, [key]);
  return [value, set];
}

// ---------------------------------------------------------------- components

export function Err({ error }) {
  if (!error) return null;
  return html`<div class="err">${error.message || String(error)}</div>`;
}

export function Dot({ state }) {
  return html`<span class=${"dot " + (state || "")}></span>`;
}

const COLORS = ["#58a6ff", "#3fb950", "#d29922", "#f85149", "#bc8cff", "#39c5cf", "#ff9bce", "#a5d6ff"];

/**
 * Time-series chart. `series`: [{label, points: [[ts, v], ...]}]; `marks`: [{ts, label, color}] (vertical lines).
 */
export function Chart({ series, height = 160, format = (v) => v, marks = [], sparkline = false }) {
  const ref = useRef(null);
  const plot = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const xs = [...new Set(series.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
    const data = [xs, ...series.map((s) => {
      const m = new Map(s.points);
      return xs.map((x) => (m.has(x) ? m.get(x) : null));
    })];
    const opts = {
      width: el.clientWidth || 300, height,
      legend: { show: false },
      cursor: sparkline ? { show: false } : { points: { size: 6 } },
      scales: { x: { time: true }, y: { range: (u, min, max) => [0, max > 0 ? max * 1.15 : 1] } },
      axes: sparkline ? [{ show: false }, { show: false }] : [
        { stroke: "#8d95a5", grid: { stroke: "#232833" }, ticks: { stroke: "#232833" } },
        { stroke: "#8d95a5", grid: { stroke: "#232833" }, ticks: { stroke: "#232833" }, size: 64,
          values: (u, ticks) => ticks.map((t) => format(t)) },
      ],
      series: [{}, ...series.map((s, i) => ({ label: s.label, stroke: s.color || COLORS[i % COLORS.length], width: 2,
        fill: sparkline ? (s.color || COLORS[i % COLORS.length]) + "22" : undefined, spanGaps: true }))],
      hooks: {
        draw: [(u) => {
          const ctx = u.ctx;
          for (const m of marks) {
            const x = u.valToPos(m.ts, "x", true);
            if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
            ctx.save();
            ctx.strokeStyle = m.color || "#d29922";
            ctx.setLineDash([4, 4]);
            ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke();
            if (m.label) {
              ctx.setLineDash([]);
              ctx.fillStyle = m.color || "#d29922";
              ctx.font = `${11 * devicePixelRatio}px system-ui`;
              ctx.fillText(m.label, x + 4 * devicePixelRatio, u.bbox.top + 12 * devicePixelRatio);
            }
            ctx.restore();
          }
        }],
      },
    };
    plot.current?.destroy();
    plot.current = new uPlot(opts, data, el);
    const resize = new ResizeObserver(() => plot.current?.setSize({ width: el.clientWidth, height }));
    resize.observe(el);
    return () => { resize.disconnect(); plot.current?.destroy(); plot.current = null; };
  }, [JSON.stringify(series), JSON.stringify(marks), height]);
  return html`<div ref=${ref} class=${sparkline ? "spark" : ""}></div>`;
}
