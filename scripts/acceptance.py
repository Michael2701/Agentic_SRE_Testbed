"""Stage 0 acceptance: every scenario run end to end through the experiment-runner, one after another.

    python3 scripts/acceptance.py [--full] [--rounds N] [scenario ...]

By default the phases are shortened like the tests do (scheduled faults are compressed with time_scale);
--full keeps the durations of the scenario files (~1 h). A run passes when it completed, the expected
symptom was seen, the system recovered and the per-domain evidence matched (where the scenario declares any).
With --rounds N every scenario runs N times and must give the same verdict each time (reproducibility).
Exit code 1 if any run fails or verdicts differ between rounds. Host side, stdlib only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

RUNNER = f"http://localhost:{os.environ.get('EXPERIMENT_RUNNER_PORT', '8091')}"
SHORT = {"baseline_s": 10, "observe_s": 25, "recovery_window_s": 5, "recovery_timeout_s": 60}
TIME_SCALE = 0.1  # scheduled faults (at_s) in short runs; the observe phase is stretched to cover them


def call(method: str, path: str, body: dict | None = None) -> dict | list:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{RUNNER}{path}", data=data, method=method,
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path}: {exc.code} {exc.read().decode()[:300]}")


def overrides(scenario: dict) -> dict:
    last_at = max((fault.get("at_s") or 0 for fault in scenario["faults"]), default=0)
    if not last_at:
        return {"durations": SHORT}
    return {"time_scale": TIME_SCALE, "durations": {**SHORT, "observe_s": round(last_at * TIME_SCALE) + SHORT["observe_s"]}}


def run(scenario: dict, full: bool) -> dict:
    body = {"scenario": scenario["name"]} | ({} if full else {"overrides": overrides(scenario)})
    experiment = call("POST", "/experiments?wait=true", body)
    verdict = experiment.get("verdict") or {}
    observe = (experiment.get("results") or {}).get("observe") or {}
    passed = (experiment["state"] == "completed" and verdict.get("expected_symptom_seen") is True
              and verdict.get("recovered") is True and verdict.get("evidence_matched") is not False)
    return {"id": experiment["id"], "passed": passed, "state": experiment["state"],
            "observed": ",".join(verdict.get("observed") or []) or "none", "evidence": verdict.get("evidence_matched"),
            "recovered": verdict.get("recovered"), "p95": observe.get("p95_s"), "errors": observe.get("error_ratio"),
            "error": experiment.get("error")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--full", action="store_true", help="use the scenario files' own durations")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("scenarios", nargs="*", help="default: all valid scenarios")
    args = parser.parse_args()

    listed = call("GET", "/scenarios")
    invalid = [s["name"] for s in listed if not s["valid"]]
    scenarios = [s for s in listed if s["valid"] and (not args.scenarios or s["name"] in args.scenarios)]
    print(f"{'round':5} {'scenario':22} {'expect':8} {'observed':14} {'p95 s':>6} {'err':>5} "
          f"{'evidence':8} {'recov':5} result")
    verdicts: dict[str, set] = {}
    failures = 0
    for round_ in range(1, args.rounds + 1):
        for scenario in scenarios:
            r = run(scenario, args.full)
            failures += not r["passed"]
            verdicts.setdefault(scenario["name"], set()).add((r["observed"], r["evidence"], r["recovered"]))
            p95 = f"{r['p95']:.2f}" if r["p95"] is not None else "-"
            errors = f"{r['errors']:.0%}" if r["errors"] is not None else "-"
            print(f"{round_:<5} {scenario['name']:22} {scenario['expect']:8} {r['observed']:14} {p95:>6} {errors:>5} "
                  f"{str(r['evidence']):8} {str(r['recovered']):5} {'PASS' if r['passed'] else 'FAIL'} "
                  f"{r['id']}{' ' + r['error'] if r['error'] else ''}", flush=True)
    unstable = sorted(name for name, seen in verdicts.items() if len(seen) > 1)
    print(f"\n{len(scenarios)} scenarios x {args.rounds} round(s): {failures} failed"
          + (f"; verdict differs between rounds: {unstable}" if unstable else "")
          + (f"; invalid scenario files: {invalid}" if invalid else ""))
    return 1 if failures or unstable or invalid else 0


if __name__ == "__main__":
    sys.exit(main())
