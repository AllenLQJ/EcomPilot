#!/usr/bin/env python
"""本机压测：自起一个 Pilot（独立测试库，用完删除），跑下单链路并发，输出延迟分位与错误率。

  ./.venv/bin/python scripts/loadtest.py --users 200 --concurrency 20

场景 A  买家链路  quote -> create -> pay（SKU_MILK_001，库存 300）
场景 B  抢最后库存  SKU_STOCK_TEST 库存 10，30 个并发下单：成功数必须恰好是 10，不超卖
数字只代表这台机器上"单个 uvicorn 进程 + 本机 MySQL"的情况，不代表生产容量。
"""
import argparse
import asyncio
import os
import socket
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pilot.core.testdb import create_test_db, drop_test_db  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else float("nan")


async def buyer(client, sem, user, timings, errors):
    async with sem:
        headers = {"Authorization": "Bearer " + (await client.post("/api/v1/dev/token", json={"user_id": user})).json()["token"]}
        t0 = time.perf_counter()
        try:
            q = await client.post("/api/v1/quotes", headers=headers, json={"items": [{"sku_id": "SKU_MILK_001", "qty": 1}]})
            q.raise_for_status()
            timings["quote"].append(time.perf_counter() - t0)
            t1 = time.perf_counter()
            o = await client.post("/api/v1/orders/create", headers=headers,
                                  json={"idempotency_key": f"k-{user}-{uuid.uuid4().hex[:8]}", "quote_id": q.json()["quote_id"]})
            o.raise_for_status()
            timings["create"].append(time.perf_counter() - t1)
            t2 = time.perf_counter()
            p = await client.post(f"/api/v1/orders/{o.json()['order_id']}/pay", headers=headers)
            p.raise_for_status()
            timings["pay"].append(time.perf_counter() - t2)
            timings["total"].append(time.perf_counter() - t0)
        except httpx.HTTPError as exc:
            response = getattr(exc, "response", None)
            try:
                code = response.json()["error"]["code"]
            except Exception:    # noqa: BLE001
                code = "-"
            errors.append(f"{type(exc).__name__}:{getattr(response, 'status_code', '-')}:{code}")


async def last_stock(client, n):
    async def one(i):
        user = f"STOCK_{i}"
        headers = {"Authorization": "Bearer " + (await client.post("/api/v1/dev/token", json={"user_id": user})).json()["token"]}
        q = await client.post("/api/v1/quotes", headers=headers, json={"items": [{"sku_id": "SKU_STOCK_TEST", "qty": 1}]})
        o = await client.post("/api/v1/orders/create", headers=headers,
                              json={"idempotency_key": f"s-{i}-{uuid.uuid4().hex[:8]}", "quote_id": q.json()["quote_id"]})
        return o.status_code, (o.json().get("error") or {}).get("code")
    return await asyncio.gather(*[one(i) for i in range(n)])


async def run(base, users, concurrency):
    timings = {k: [] for k in ("quote", "create", "pay", "total")}
    errors: list[str] = []
    limits = httpx.Limits(max_connections=concurrency * 2)
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as client:
        started = time.perf_counter()
        sem = asyncio.Semaphore(concurrency)
        await asyncio.gather(*[buyer(client, sem, f"LT_{i}", timings, errors) for i in range(users)])
        wall = time.perf_counter() - started
        stock = await last_stock(client, 30)
    ok = len(timings["total"])
    print(f"\n场景 A  买家链路  用户={users} 并发={concurrency}  成功={ok} 失败={len(errors)} "
          f"耗时={wall:.1f}s 吞吐={ok / wall:.1f} 链路/s")
    print(f"{'阶段':8}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}   (ms)")
    for name, values in timings.items():
        if values:
            print(f"{name:8}{pct(values, .5) * 1000:9.1f}{pct(values, .95) * 1000:9.1f}{pct(values, .99) * 1000:9.1f}{max(values) * 1000:9.1f}")
    if errors:
        print("错误分布:", {e: errors.count(e) for e in set(errors)})
    created = sum(1 for status, _ in stock if status in (200, 201))
    codes = {}
    for status, code in stock:
        codes[(status, code)] = codes.get((status, code), 0) + 1
    print(f"\n场景 B  抢最后库存  库存=10 并发下单=30  成功={created}  结果分布={codes}")
    oversold = created > 10
    print("超卖!" if oversold else ("结论: 未超卖" + ("" if created == 10 else f"（但只成功 {created} 单，有 {10 - created} 件库存被误拒）")))
    return 1 if oversold or errors else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()
    db = create_test_db()
    port = free_port()
    env = {**os.environ, "ECOMPILOT_DB_NAME": db, "ECOMPILOT_DEV_TOKENS": "1", "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "pilot.main:app", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            print("Pilot 没起来")
            return 2
        return asyncio.run(run(f"http://127.0.0.1:{port}", args.users, args.concurrency))
    finally:
        proc.terminate()
        proc.wait(10)
        drop_test_db(db)


if __name__ == "__main__":
    sys.exit(main())
