"""Steady client traffic through nginx during an experiment, with every request recorded.

Same mix and User-Agent as `tests/load.py` (`make load`), so experiment traffic is indistinguishable from
ordinary load in the diagnostic plane. Failed logins are background noise and are left out of the stats.
"""

import asyncio
import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass

import httpx

USER_AGENT = "shop-client/1.0"
USERS = [("alice", "alice"), ("bob", "bob")]


@dataclass(frozen=True)
class Sample:
    at: float  # wall clock, comparable with Prometheus timestamps
    kind: str  # create_order | get_order | bad_login | skipped (max_in_flight reached, nothing sent)
    status: int  # 0 = transport error (no HTTP response)
    seconds: float


def _quantile(values: list[float], q: float) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1]


def summarize(samples: list[Sample]) -> dict:
    """Stats of order requests (create + get); `skipped` counts ticks the capped client couldn't send."""
    skipped = sum(s.kind == "skipped" for s in samples)
    orders = [s for s in samples if s.kind not in ("bad_login", "skipped")]
    latencies = [s.seconds for s in orders]
    count = len(orders)
    span = (orders[-1].at - orders[0].at) if count > 1 else 0.0
    return {
        "requests": count,
        "rps": round(count / span, 2) if span else 0.0,
        "p50_s": round(_quantile(latencies, 0.50), 4),
        "p95_s": round(_quantile(latencies, 0.95), 4),
        "p99_s": round(_quantile(latencies, 0.99), 4),
        "error_ratio": round(sum(s.status >= 500 or s.status == 0 for s in orders) / count, 4) if count else 0.0,
        "auth_error_ratio": round(sum(s.status == 401 for s in orders) / count, 4) if count else 0.0,
        "skipped": skipped,
        "statuses": dict(sorted(Counter(f"{s.kind} {s.status or 'transport_error'}" for s in orders).items())),
    }


class Traffic:
    def __init__(self, base_url: str, rate: float, max_in_flight: int | None = None):
        self.base_url = base_url
        self.rate = rate
        self.max_in_flight = max_in_flight
        self.samples: list[Sample] = []
        self._inflight: dict[int, float] = {}  # request key -> wall-clock start
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    def window(self, start: float, end: float) -> list[Sample]:
        return [s for s in self.samples if start <= s.at < end]

    async def settle(self, before: float, timeout: float = 11) -> None:
        """Waits until requests started before `before` finished, so slow ones count in their window."""
        deadline = time.monotonic() + timeout
        while any(at < before for at in self._inflight.values()) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

    async def start(self) -> None:
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10, headers={"User-Agent": USER_AGENT})
        tokens = {}
        for user, password in USERS:
            response = await self._client.post("/login", json={"username": user, "password": password})
            response.raise_for_status()
            tokens[user] = response.json()["access_token"]
        self._task = asyncio.create_task(self._run(tokens))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    async def _run(self, tokens: dict) -> None:
        order_ids: list[tuple[str, str]] = []
        pending: set[asyncio.Task] = set()
        interval = 1 / self.rate
        next_tick = time.monotonic()
        try:
            while True:
                if self.max_in_flight and len(pending) >= self.max_in_flight:
                    self.samples.append(Sample(time.time(), "skipped", -1, 0.0))
                else:
                    task = asyncio.create_task(self._one(tokens, order_ids))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                next_tick += interval
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        finally:
            for task in pending:
                task.cancel()

    async def _one(self, tokens: dict, order_ids: list) -> None:
        roll = random.random()
        user, _ = random.choice(USERS)
        headers = {"Authorization": f"Bearer {tokens[user]}"}
        own = [order_id for owner, order_id in order_ids if owner == user]
        started, wall = time.perf_counter(), time.time()
        key = id(asyncio.current_task())
        self._inflight[key] = wall
        status = 0
        if roll < 0.05:
            kind = "bad_login"
        elif roll < 0.20 and own:
            kind = "get_order"
        else:
            kind = "create_order"
        try:
            if kind == "bad_login":
                response = await self._client.post("/login", json={"username": user, "password": "wrong"})
            elif kind == "get_order":
                response = await self._client.get(f"/orders/{random.choice(own)}", headers=headers)
            else:
                body = {"item": random.choice(["book", "pen", "mug", "lamp"]), "quantity": random.randint(1, 3),
                        "amount_cents": random.randint(100, 10_000)}
                response = await self._client.post("/orders", json=body, headers=headers)
                if response.status_code == 201:
                    order_ids.append((user, response.json()["id"]))
                    del order_ids[:-500]
            status = response.status_code
        except httpx.HTTPError:
            pass
        finally:
            self._inflight.pop(key, None)
        self.samples.append(Sample(wall, kind, status, time.perf_counter() - started))
