"""SQLite persistence for faults (stdlib only; the file lives on the `faultdata` volume)."""

from __future__ import annotations  # `list` below is also a method name

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS faults (
    id            TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    type          TEXT NOT NULL,
    target        TEXT NOT NULL,
    parameters    TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('active', 'removed', 'failed')),
    error         TEXT,
    -- failed, and undoing what it partially applied failed too: DELETE retries the revert (main.py)
    needs_cleanup INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS faults_state_idx ON faults (state);
-- Env/command/restart policy of a container as compose created it, saved before the first redeploy
-- (see deploy.py).
CREATE TABLE IF NOT EXISTS baselines (
    service TEXT PRIMARY KEY,
    env     TEXT NOT NULL,
    cmd     TEXT NOT NULL,
    restart TEXT NOT NULL
);
-- CPU quota (NanoCpus) of a container as compose created it, saved before cpu_limit lowers it (limits.py).
CREATE TABLE IF NOT EXISTS cpu_baselines (
    service   TEXT PRIMARY KEY,
    nano_cpus INTEGER NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FaultStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(faults)")}
        if "needs_cleanup" not in columns:  # a faults.db from before M10
            self._db.execute("ALTER TABLE faults ADD COLUMN needs_cleanup INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        fault = dict(row)
        fault["parameters"] = json.loads(fault["parameters"])
        fault["needs_cleanup"] = bool(fault["needs_cleanup"])
        return fault

    def insert(self, fault: dict) -> dict:
        self._db.execute(
            "INSERT INTO faults (id, experiment_id, type, target, parameters, state, error, created_at, updated_at)"
            " VALUES (:id, :experiment_id, :type, :target, :parameters, :state, :error, :created_at, :updated_at)",
            {**fault, "parameters": json.dumps(fault["parameters"])},
        )
        return self.get(fault["id"])

    def get(self, fault_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM faults WHERE id = ?", (fault_id,)).fetchone()
        return self._to_dict(row) if row else None

    def list(self, state: str | None = None) -> list[dict]:
        if state:
            rows = self._db.execute("SELECT * FROM faults WHERE state = ? ORDER BY created_at", (state,))
        else:
            rows = self._db.execute("SELECT * FROM faults ORDER BY created_at")
        return [self._to_dict(row) for row in rows]

    def update(self, fault_id: str, state: str, error: str | None = None, needs_cleanup: bool = False) -> dict:
        self._db.execute(
            "UPDATE faults SET state = ?, error = ?, needs_cleanup = ?, updated_at = ? WHERE id = ?",
            (state, error, needs_cleanup, now(), fault_id),
        )
        return self.get(fault_id)

    def pending_cleanup(self) -> list[dict]:
        return [fault for fault in self.list("failed") if fault["needs_cleanup"]]

    def save_baseline(self, service: str, env: list[str], cmd: list[str], restart: dict) -> None:
        self._db.execute("INSERT OR REPLACE INTO baselines VALUES (?, ?, ?, ?)",
                         (service, json.dumps(env), json.dumps(cmd), json.dumps(restart)))

    def baseline(self, service: str) -> tuple[list[str], list[str], dict] | None:
        row = self._db.execute("SELECT env, cmd, restart FROM baselines WHERE service = ?", (service,)).fetchone()
        return (json.loads(row["env"]), json.loads(row["cmd"]), json.loads(row["restart"])) if row else None

    def delete_baseline(self, service: str) -> None:
        self._db.execute("DELETE FROM baselines WHERE service = ?", (service,))

    def save_cpu_baseline(self, service: str, nano_cpus: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO cpu_baselines VALUES (?, ?)", (service, nano_cpus))

    def cpu_baseline(self, service: str) -> int | None:
        row = self._db.execute("SELECT nano_cpus FROM cpu_baselines WHERE service = ?", (service,)).fetchone()
        return row["nano_cpus"] if row else None

    def delete_cpu_baseline(self, service: str) -> None:
        self._db.execute("DELETE FROM cpu_baselines WHERE service = ?", (service,))
