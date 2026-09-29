"""Minimal steady traffic generator: logins and orders through nginx.

Usage: python load.py [--rate 5] [--duration 60]
Mix per tick: ~80% POST /orders, ~15% GET /orders/{id}, ~5% failed logins (normal background noise).
"""

import argparse
import asyncio
import os
import random
import time
from collections import Counter

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
USERS = [("alice", "alice"), ("bob", "bob")]


async def login(client: httpx.AsyncClient, username: str, password: str) -> str | None:
    response = await client.post("/login", json={"username": username, "password": password})
    return response.json()["access_token"] if response.status_code == 200 else None


async def one_request(client: httpx.AsyncClient, tokens: dict, order_ids: list, stats: Counter) -> None:
    roll = random.random()
    user, password = random.choice(USERS)
    try:
        if roll < 0.05:
            response = await client.post("/login", json={"username": user, "password": "wrong"})
            stats[f"login {response.status_code}"] += 1
            return
        headers = {"Authorization": f"Bearer {tokens[user]}"}
        own_orders = [order_id for owner, order_id in order_ids if owner == user]
        if roll < 0.20 and own_orders:
            response = await client.get(f"/orders/{random.choice(own_orders)}", headers=headers)
            stats[f"get_order {response.status_code}"] += 1
            return
        body = {"item": random.choice(["book", "pen", "mug", "lamp"]), "quantity": random.randint(1, 3),
                "amount_cents": random.randint(100, 10_000)}
        response = await client.post("/orders", json=body, headers=headers)
        stats[f"create_order {response.status_code}"] += 1
        if response.status_code == 201:
            order_ids.append((user, response.json()["id"]))
            del order_ids[:-500]
    except httpx.HTTPError as exc:
        stats[f"transport_error {type(exc).__name__}"] += 1


async def main(rate: float, duration: float) -> None:
    stats: Counter = Counter()
    order_ids: list = []
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        tokens = {user: await login(client, user, password) for user, password in USERS}
        if not all(tokens.values()):
            raise SystemExit(f"login failed for some demo users: {tokens}")

        print(f"load: {rate} req/s for {duration}s against {BASE_URL}")
        deadline = time.monotonic() + duration
        interval = 1 / rate
        tasks = set()
        next_tick = time.monotonic()
        while time.monotonic() < deadline:
            task = asyncio.create_task(one_request(client, tokens, order_ids, stats))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            next_tick += interval
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        await asyncio.gather(*tasks)

    for key, count in sorted(stats.items()):
        print(f"  {key:<32} {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=5, help="requests per second")
    parser.add_argument("--duration", type=float, default=60, help="seconds")
    args = parser.parse_args()
    asyncio.run(main(args.rate, args.duration))
