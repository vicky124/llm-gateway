"""Fire concurrent requests at a running gateway to populate the dashboard.

    python scripts/loadtest.py --url http://localhost:8080 --key sk-gw-local-dev --n 200
"""

import argparse
import asyncio
import random
import time
from collections import Counter

import httpx

PROMPTS = ["Summarize CAP theorem", "Explain idempotency", "What is a circuit breaker?",
           "Define backpressure", "Explain token buckets"]


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--key", default="sk-gw-local-dev")
    p.add_argument("--route", default="demo")
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=10)
    args = p.parse_args()

    codes: Counter[int] = Counter()
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=args.url, timeout=120,
                                 headers={"Authorization": f"Bearer {args.key}"}) as client:
        async def one() -> None:
            async with sem:
                body = {"model": args.route, "max_tokens": 256,
                        "messages": [{"role": "user", "content": random.choice(PROMPTS)}]}
                res = await client.post("/v1/chat", json=body)
                codes[res.status_code] += 1

        start = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(args.n)))
    print(f"{args.n} requests in {time.perf_counter() - start:.1f}s -> {dict(codes)}")


if __name__ == "__main__":
    asyncio.run(main())
