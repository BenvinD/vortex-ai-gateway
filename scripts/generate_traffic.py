"""Put something on the dashboard: mixed traffic against a running gateway.

An empty Grafana dashboard proves nothing — every panel reads "No data" whether
the query is right or the gateway is not publishing the series. This sends a
deliberately *mixed* load so that every panel has to draw something, and so the
ones that stay empty are telling you about a real gap.

The mix, and why each part is in it:

* **Repeats.** A small pool of prompts, sent more than once, so the exact cache
  has something to hit on. Without repeats the hit-rate panel is a flat zero
  that looks identical to a broken cache.
* **Several models.** So the by-model breakdowns have more than one series, and
  so the label budget is exercised (``--models`` above the budget is how you
  watch ``other`` appear — ADR-027).
* **Streams.** The TTFT histogram has no other source: a buffered response is
  deliberately not timed for first token.
* **Bad requests.** A model nothing routes to, and a body the contract rejects.
  An error panel that has never seen an error is not a working error panel.
* **Bypasses.** So the `BYPASS` series exists and it is visible that bypasses
  are excluded from the hit-rate denominator.

Usage, with the stack from `compose.yaml` running:

    uv run scripts/generate_traffic.py
    uv run scripts/generate_traffic.py --requests 2000 --concurrency 32

It prints a summary read back from ``/metrics``, which doubles as a check that
the gateway is publishing what the dashboard asks for.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

#: Prompts are drawn from this pool with repetition, which is what produces
#: cache hits. A larger pool means a lower hit rate; that is the knob.
PROMPTS: tuple[str, ...] = (
    "What is the capital of France?",
    "Explain a circuit breaker in one paragraph.",
    "How do I reverse a list in Python?",
    "Summarise the CAP theorem.",
    "Why is the sky blue?",
    "What does HTTP 429 mean?",
    "Write a haiku about latency.",
    "Difference between a mutex and a semaphore?",
)

#: Kept short so the default run stays inside a single Prometheus retention
#: window and the dashboard's 15-minute view shows the whole thing.
MODELS: tuple[str, ...] = ("gpt-4o-mini", "claude-3-5-haiku", "llama3.1-8b")

CHAT_PATH = "/v1/chat/completions"

#: Any well-formed bearer token is accepted while no allow-list is configured,
#: which is how the compose stack is set up.
AUTH = {"Authorization": "Bearer traffic-generator"}


@dataclass(frozen=True, slots=True)
class Sample:
    """One line of the Prometheus text format, taken apart."""

    name: str
    labels: dict[str, str]
    value: float


@dataclass
class Tally:
    """What the generator saw, from the client's side of the wire."""

    statuses: Counter[str] = field(default_factory=Counter)
    cache: Counter[str] = field(default_factory=Counter)
    kinds: Counter[str] = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)

    def record(self, kind: str, status: int, cache: str | None, seconds: float) -> None:
        self.kinds[kind] += 1
        self.statuses[str(status)] += 1
        self.cache[cache or "none"] += 1
        self.latencies.append(seconds)

    def quantile(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        return ordered[min(int(q * len(ordered)), len(ordered) - 1)]


def body(model: str, prompt: str, **extra: object) -> dict[str, object]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": 64,
    } | extra


async def one_request(
    client: httpx.AsyncClient, rng: random.Random, models: tuple[str, ...], tally: Tally
) -> None:
    """Send one request of a randomly chosen kind and record what came back.

    The weights are the mix: mostly ordinary buffered completions, a steady
    trickle of streams, and a few per cent of each of the things that are only
    interesting because they are rare.
    """
    kind = rng.choices(
        ("buffered", "stream", "bypass", "unroutable", "malformed"),
        weights=(70, 18, 6, 4, 2),
    )[0]
    model = rng.choice(models)
    prompt = rng.choice(PROMPTS)
    started = time.perf_counter()

    try:
        if kind == "stream":
            async with client.stream(
                "POST", CHAT_PATH, json=body(model, prompt, stream=True)
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
                tally.record(
                    kind,
                    response.status_code,
                    response.headers.get("x-cache"),
                    time.perf_counter() - started,
                )
            return

        if kind == "bypass":
            response = await client.post(
                CHAT_PATH,
                json=body(model, prompt),
                headers={"X-Vortex-Cache-Bypass": "true"},
            )
        elif kind == "unroutable":
            response = await client.post(CHAT_PATH, json=body("no-such-model-anywhere", prompt))
        elif kind == "malformed":
            # `extra="forbid"` on the contract, so a misspelt field is a 422
            # naming the offending key rather than a silently ignored one
            # (ADR-010).
            response = await client.post(CHAT_PATH, json=body(model, prompt, temperture=0.7))
        else:
            response = await client.post(CHAT_PATH, json=body(model, prompt))

        tally.record(
            kind,
            response.status_code,
            response.headers.get("x-cache"),
            time.perf_counter() - started,
        )
    except httpx.HTTPError as exc:
        tally.errors[type(exc).__name__] += 1


async def run(args: argparse.Namespace) -> Tally:
    """Fire ``args.requests`` requests, ``args.concurrency`` of them at a time."""
    rng = random.Random(args.seed)
    tally = Tally()
    limit = asyncio.Semaphore(args.concurrency)
    models = MODELS[: args.models]

    async with httpx.AsyncClient(
        base_url=args.url, headers=AUTH, timeout=httpx.Timeout(60.0, connect=5.0)
    ) as client:

        async def worker() -> None:
            async with limit:
                await one_request(client, rng, models, tally)
                if args.delay:
                    await asyncio.sleep(args.delay)

        await asyncio.gather(*(worker() for _ in range(args.requests)))
    return tally


async def scrape(url: str) -> list[Sample]:
    """The gateway's own counters, parsed from the text exposition format.

    Read back rather than trusted from this side of the wire, because the point
    of the exercise is the *gateway's* numbers: a client-side tally that
    disagrees with ``/metrics`` means the instrumentation is wrong, which is
    exactly the failure a dashboard cannot show you.

    The labels are parsed into a mapping rather than matched as a substring of
    the line. ``prometheus_client`` writes them in whatever order the metric
    declared, and a reader that assumes the order silently finds nothing — it
    reports zero for a series that is right there, which reads as "the gateway
    is not publishing it" and sends you to debug the wrong end.
    """
    async with httpx.AsyncClient(base_url=url, timeout=10.0) as client:
        response = await client.get("/metrics")
        response.raise_for_status()

    samples: list[Sample] = []
    for line in response.text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        try:
            number = float(value)
        except ValueError:
            continue
        name, _, rest = head.partition("{")
        samples.append(Sample(name.strip(), _labels(rest.rstrip().rstrip("}")), number))
    return samples


def _labels(text: str) -> dict[str, str]:
    """``a="1",b="2"`` as a mapping. Label values here never contain a comma."""
    pairs: dict[str, str] = {}
    for part in text.split(","):
        key, _, value = part.partition("=")
        if key:
            pairs[key.strip()] = value.strip().strip('"')
    return pairs


def total(samples: Sequence[Sample], name: str, **match: str) -> float:
    """Every sample called ``name`` whose labels include ``match``, added up."""
    return sum(
        s.value
        for s in samples
        if s.name == name and all(s.labels.get(k) == v for k, v in match.items())
    )


def report(tally: Tally, samples: Sequence[Sample], elapsed: float) -> None:
    sent = sum(tally.kinds.values())
    print(f"\n  sent {sent} requests in {elapsed:.1f}s ({sent / max(elapsed, 0.001):.0f}/s)\n")

    print("  client saw")
    for label, counter in (
        ("kind", tally.kinds),
        ("status", tally.statuses),
        ("X-Cache", tally.cache),
    ):
        pairs = ", ".join(f"{k}={v}" for k, v in sorted(counter.items()))
        print(f"    {label:9} {pairs}")
    print(
        f"    {'latency':9} p50={tally.quantile(0.5) * 1000:.0f}ms"
        f" p95={tally.quantile(0.95) * 1000:.0f}ms"
    )
    if tally.errors:
        print(f"    {'failed':9} {dict(tally.errors)}")

    hits = total(samples, "vortex_cache_lookups_total", tier="exact", outcome="HIT")
    misses = total(samples, "vortex_cache_lookups_total", tier="exact", outcome="MISS")
    bypasses = total(samples, "vortex_cache_lookups_total", tier="exact", outcome="BYPASS")
    attempts = total(samples, "vortex_provider_attempts_total")
    chat = total(samples, "vortex_requests_total", route=CHAT_PATH)

    print("\n  gateway published")
    print(
        f"    {'requests':9} {total(samples, 'vortex_requests_total'):.0f} total,"
        f" {chat:.0f} on the chat route"
    )
    if hits + misses:
        print(
            f"    {'cache':9} {hits:.0f} hits / {hits + misses:.0f} lookups"
            f" = {hits / (hits + misses):.1%}, and {bypasses:.0f} bypasses that"
            " are in neither"
        )
    if attempts:
        print(
            f"    {'upstream':9} {attempts:.0f} attempts = {attempts / chat:.2f} calls per request"
        )
    else:
        # The mock provider is mounted directly, not behind `ResilientProvider`,
        # so there is nothing to count attempts. Configure VORTEX_MODEL_ROUTES
        # to put a real adapter — and its retry loop — in the path.
        print(f"    {'upstream':9} no wrapped provider; attempts are not counted")
    print(f"    {'tokens':9} {total(samples, 'vortex_tokens_total'):.0f}")
    print(f"    {'cost':9} ${total(samples, 'vortex_cost_usd_total'):.6f}")
    print(
        f"    {'streams':9} {total(samples, 'vortex_streams_total'):.0f},"
        f" {total(samples, 'vortex_stream_ttft_seconds_count'):.0f} with a TTFT observation"
    )
    models = sorted(
        {
            s.labels["model"]
            for s in samples
            if s.name == "vortex_requests_total" and s.labels.get("route") == CHAT_PATH
        }
    )
    print(f"    {'models':9} {', '.join(models)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8000", help="gateway base URL")
    parser.add_argument("--requests", type=int, default=600, help="how many to send")
    parser.add_argument("--concurrency", type=int, default=16, help="how many at once")
    parser.add_argument(
        "--models",
        type=int,
        default=len(MODELS),
        choices=range(1, len(MODELS) + 1),
        help="how many distinct model names to use",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to pause after each request; use it to spread a run out "
        "over a longer window so the dashboard has more than one point",
    )
    parser.add_argument("--seed", type=int, default=1729, help="so a run is repeatable")
    args = parser.parse_args()

    started = time.perf_counter()
    try:
        tally = asyncio.run(run(args))
        samples = asyncio.run(scrape(args.url))
    except httpx.HTTPError as exc:
        print(f"could not reach the gateway at {args.url}: {exc}", file=sys.stderr)
        return 1

    report(tally, samples, time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
