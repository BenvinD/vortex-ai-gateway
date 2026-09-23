"""Turn a matrix run into the table that goes in the README.

``bench/run-matrix.sh`` leaves one k6 summary per arm in its results directory,
plus a ``/metrics`` scrape either side of each run. This reads all of it and
prints two things:

* the **markdown table** — one row per arm, with the five columns the README
  promises: p50, p95, p99, requests per second, error rate;
* the **cross-check** — the client's numbers next to the gateway's own, for the
  quantities both of them measure. That pairing is the point rather than a
  nicety: Day 12's lesson was that a monitoring path fails quietly and
  plausibly, so a client-side TTFT that disagrees with
  ``vortex_stream_ttft_seconds`` is a bug in one of the two instruments, and
  neither number is worth publishing until they agree.

Usage::

    uv run bench/report.py bench/results/20260923T101500Z
    uv run bench/report.py bench/results/20260923T101500Z --markdown-only
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The order arms appear in, so two runs produce comparable tables. Anything
#: not named here sorts after, alphabetically, rather than being dropped — a
#: report that silently omits a workload is worse than an ugly one.
WORKLOAD_ORDER = ("baseline", "cache-hit", "streaming", "provider-failure")


@dataclass(frozen=True, slots=True)
class Arm:
    """One cell of the matrix, parsed out of its file name.

    The label is built by ``run-matrix.sh`` as
    ``<workload>-w<workers>-sem<off|on>``, which is the one place the naming
    lives. Parsed rather than recorded inside the JSON because k6 writes that
    file and does not know what arm it is running.
    """

    workload: str
    workers: int
    semantic: str
    summary: dict[str, Any]
    metrics_after: str

    @property
    def sort_key(self) -> tuple[int, str, int, str]:
        rank = WORKLOAD_ORDER.index(self.workload) if self.workload in WORKLOAD_ORDER else 99
        return (rank, self.workload, self.workers, self.semantic)


def parse_label(name: str) -> tuple[str, int, str] | None:
    """``baseline-w4-semon`` → ``("baseline", 4, "on")``, or ``None``."""
    head, _, semantic = name.rpartition("-sem")
    workload, _, workers = head.rpartition("-w")
    if not workload or not workers.isdigit() or semantic not in {"on", "off"}:
        return None
    return workload, int(workers), semantic


def load(results: Path) -> list[Arm]:
    """Every arm in ``results``, newest-run-agnostic and order-independent."""
    arms: list[Arm] = []
    for path in sorted(results.glob("*.json")):
        parsed = parse_label(path.stem)
        if parsed is None:
            continue
        workload, workers, semantic = parsed
        try:
            summary = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! {path.name}: {exc}", file=sys.stderr)
            continue
        scrape = results / f"{path.stem}.metrics.after.txt"
        arms.append(
            Arm(
                workload=workload,
                workers=workers,
                semantic=semantic,
                summary=summary,
                metrics_after=scrape.read_text() if scrape.exists() else "",
            )
        )
    return sorted(arms, key=lambda arm: arm.sort_key)


def stat(summary: dict[str, Any], metric: str, key: str) -> float | None:
    """One statistic out of a k6 summary, or ``None`` if the metric is absent.

    Absent is a real answer here: a workload that recorded no streams has no
    TTFT trend, and reporting zero for it would put a measurement that was
    never taken on the same table as the ones that were.
    """
    values = summary.get("metrics", {}).get(metric, {}).get("values", {})
    value = values.get(key)
    return float(value) if isinstance(value, int | float) else None


def ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def gauge(scrape: str, name: str) -> float:
    """Sum every sample of ``name`` in a Prometheus text scrape.

    Summed over the label sets rather than matched against one, because with
    four workers the scrape reaching us is whichever worker the OS gave the
    connection to, and the label order is the metric's declaration order — the
    bug docs/notes/day-12.md is about. A prefix match would silently find
    nothing.
    """
    total = 0.0
    for line in scrape.splitlines():
        if line.startswith("#") or not line:
            continue
        head, _, raw = line.rpartition(" ")
        series = head.partition("{")[0].strip()
        if series != name:
            continue
        try:
            total += float(raw)
        except ValueError:
            continue
    return total


def markdown(arms: list[Arm]) -> str:
    """The README table."""
    lines = [
        "| Workload | Workers | Semantic | p50 | p95 | p99 | RPS | Errors |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        summary = arm.summary
        lines.append(
            f"| {arm.workload} | {arm.workers} | {arm.semantic} "
            f"| {ms(stat(summary, 'http_req_duration', 'med'))} ms "
            f"| {ms(stat(summary, 'http_req_duration', 'p(95)'))} ms "
            f"| {ms(stat(summary, 'http_req_duration', 'p(99)'))} ms "
            f"| {stat(summary, 'http_reqs', 'rate') or 0:.0f} "
            f"| {rate(stat(summary, 'http_req_failed', 'rate'))} |"
        )
    return "\n".join(lines)


def scaling(arms: list[Arm]) -> str:
    """One worker against four, per workload: the question the axis exists for."""
    by_key: dict[tuple[str, str, int], Arm] = {
        (arm.workload, arm.semantic, arm.workers): arm for arm in arms
    }
    rows = []
    for workload, semantic, workers in sorted(by_key):
        if workers != 1:
            continue
        four = by_key.get((workload, semantic, 4))
        if four is None:
            continue
        one_rps = stat(by_key[workload, semantic, 1].summary, "http_reqs", "rate") or 0.0
        four_rps = stat(four.summary, "http_reqs", "rate") or 0.0
        if one_rps <= 0:
            continue
        # Against 4.0, not against 1.0: the interesting number is how much of
        # the four processes you actually got, and "2.3x" reads as a success
        # until you remember what was paid for it.
        rows.append(
            f"    {workload:<18} sem={semantic:<3} "
            f"{one_rps:8.0f}/s → {four_rps:8.0f}/s   "
            f"{four_rps / one_rps:.2f}x of a possible 4.00x"
        )
    return "\n".join(rows)


def cross_check(arms: list[Arm]) -> str:
    """The client's numbers against the gateway's, where both measured the same thing.

    Two pairs are worth the comparison. **Request count**: k6's ``http_reqs``
    against ``vortex_requests_total``, which catches a run that was partly
    answered by something other than the gateway under test. **TTFT**: k6's
    time to first byte on an SSE response against the gateway's own
    ``vortex_stream_ttft_seconds``, which are two independent measurements of
    one quantity and should agree to within the loopback round trip.
    """
    rows = []
    for arm in arms:
        if not arm.metrics_after:
            continue
        client_reqs = stat(arm.summary, "http_reqs", "count") or 0.0
        gateway_reqs = gauge(arm.metrics_after, "vortex_requests_total")
        label = f"{arm.workload}-w{arm.workers}-sem{arm.semantic}"

        line = f"    {label:<32} requests  client {client_reqs:.0f}  gateway {gateway_reqs:.0f}"
        # k6 counts the warmup pass too, and the gateway counts its own health
        # probes, so these are never identical. An order-of-magnitude gap is the
        # thing worth seeing.
        if gateway_reqs > 0 and not 0.5 <= client_reqs / gateway_reqs <= 2.0:
            line += "   ← investigate"
        rows.append(line)

        client_ttft = stat(arm.summary, "vortex_ttft_ms", "avg")
        count = gauge(arm.metrics_after, "vortex_stream_ttft_seconds_count")
        if client_ttft is not None and count > 0:
            gateway_ttft = gauge(arm.metrics_after, "vortex_stream_ttft_seconds_sum") / count * 1000
            rows.append(
                f"    {'':<32} ttft      client {client_ttft:.2f}ms  gateway {gateway_ttft:.2f}ms"
            )
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", type=Path, help="a bench/results/<timestamp> directory")
    parser.add_argument(
        "--markdown-only",
        action="store_true",
        help="print just the table, for pasting into the README",
    )
    args = parser.parse_args()

    if not args.results.is_dir():
        print(f"not a directory: {args.results}", file=sys.stderr)
        return 1

    arms = load(args.results)
    if not arms:
        print(f"no arm summaries in {args.results}", file=sys.stderr)
        return 1

    print(markdown(arms))
    if args.markdown_only:
        return 0

    manifest = args.results / "manifest.txt"
    if manifest.exists():
        print("\n  measured on\n")
        for line in manifest.read_text().splitlines():
            print(f"    {line}")

    scale = scaling(arms)
    if scale:
        print("\n  workers\n")
        print(scale)

    checks = cross_check(arms)
    if checks:
        print("\n  cross-check — the client's count against the gateway's\n")
        print(checks)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
