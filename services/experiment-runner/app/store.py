"""SQLite persistence for experiments (stdlib only; the file lives on the `experimentdata` volume).

One JSON document per experiment; `state` is also a column for filtering. IDs are sequential (`exp-<n>`).
"""

from __future__ import annotations  # `list` below is also a method name

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT UNIQUE,
    state      TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed', 'aborted')),
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ExperimentStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def create(self, doc: dict) -> dict:
        created = now()
        seq = self._db.execute("INSERT INTO experiments (state, created_at, doc) VALUES ('running', ?, '{}')",
                               (created,)).lastrowid
        experiment = {"id": f"exp-{seq}", **doc, "state": "running", "created_at": created, "updated_at": created}
        self._db.execute("UPDATE experiments SET id = ?, doc = ? WHERE seq = ?",
                         (experiment["id"], json.dumps(experiment), seq))
        return experiment

    def save(self, experiment: dict) -> dict:
        experiment["updated_at"] = now()
        self._db.execute("UPDATE experiments SET state = ?, doc = ? WHERE id = ?",
                         (experiment["state"], json.dumps(experiment), experiment["id"]))
        return experiment

    def get(self, experiment_id: str) -> dict | None:
        row = self._db.execute("SELECT doc FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        return json.loads(row["doc"]) if row else None

    def list(self, state: str | None = None) -> list[dict]:
        if state:
            rows = self._db.execute("SELECT doc FROM experiments WHERE state = ? ORDER BY seq", (state,))
        else:
            rows = self._db.execute("SELECT doc FROM experiments ORDER BY seq")
        return [json.loads(row["doc"]) for row in rows]
