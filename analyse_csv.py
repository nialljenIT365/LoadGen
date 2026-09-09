"""Derive convergence, steady-state error and excursions from a LoadGen CSV.

The CSV is written by `loadgen.py --log PATH`, one row per 2 s tick:

    timestamp, users, gpu_t, gpu_a, gpu_nvml, gpu_duty, vram_t, vram_a,
    cpu_t, cpu_a, ram_t, ram_a

`gpu_duty` is the gpu Dial's own busy fraction, 0 to 1. It is what the Dial
controls; `gpu_a` is what the counter reads back. A duty that never moves while
`gpu_a` sits on Target means the Dial was seeded correctly and never had to
correct, which is not the same result as a loop that converged.

An Actual is written as an empty field when the reading was unavailable and as
a number when it was read. `0.0` and `` are different answers: the first says
the instrument reported an idle GPU, the second says the instrument failed.
This script counts them separately, because that is the distinction step 0 of
the verification brief turns on.

Usage:

    python analyse_csv.py analysis\\gpu50.csv
    python analyse_csv.py analysis\\all.csv --dial gpu
    python analyse_csv.py analysis\\*.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import statistics
import sys

TICK_SECONDS = 2.0

# dial -> (steady-state tolerance, seconds allowed to converge)
TOLERANCE = {
    "gpu": (10.0, 20.0),
    "vram": (3.0, 2.0),
    "cpu": (5.0, 10.0),
    "ram": (2.0, 10.0),
}


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def to_float(text):
    """None when the field was blank, i.e. the reading was unavailable."""
    if text is None or text.strip() == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def segments(rows, dial):
    """Split into runs of constant Target, so live retargeting is judged apart.

    Yields (start_index, target, [(index, actual), ...]).
    """
    current = None
    start = 0
    bucket = []
    for i, row in enumerate(rows):
        target = to_float(row.get(f"{dial}_t"))
        if target is None:
            continue
        if current is None or target != current:
            if bucket:
                yield start, current, bucket
            current, start, bucket = target, i, []
        bucket.append((i, to_float(row.get(f"{dial}_a"))))
    if bucket:
        yield start, current, bucket


def analyse_segment(dial, target, samples, skip_first):
    tol, converge_s = TOLERANCE[dial]
    if skip_first:
        # psutil.cpu_percent measures the interval since the previous call, and
        # for the first tick that interval covers process startup.
        samples = samples[1:]
    if not samples:
        return None

    blanks = sum(1 for _, a in samples if a is None)
    read = [(i, a) for i, a in samples if a is not None]
    zeros = sum(1 for _, a in read if a == 0.0) if target > 0.0 else 0

    result = {
        "target": target,
        "ticks": len(samples),
        "blank": blanks,
        "hard_zero": zeros,
        "converged_tick": None,
        "converged_s": None,
        "mean_error": None,
        "sd_error": None,
        "excursions": 0,
        "worst_excursion": None,
    }
    if not read:
        return result

    # Convergence: the first sample that is inside tolerance and stays inside
    # for every remaining sample. A reading that leaves tolerance later has not
    # converged, it has merely passed through.
    base = read[0][0]
    for pos, (idx, _) in enumerate(read):
        if all(abs(a - target) <= tol for _, a in read[pos:]):
            result["converged_tick"] = idx
            result["converged_s"] = (idx - base) * TICK_SECONDS
            break

    tail = read if result["converged_tick"] is None else [
        (i, a) for i, a in read if i >= result["converged_tick"]
    ]
    errors = [a - target for _, a in tail]
    if errors:
        result["mean_error"] = statistics.fmean(errors)
        result["sd_error"] = statistics.pstdev(errors) if len(errors) > 1 else 0.0

    outside = [a - target for _, a in read if abs(a - target) > tol]
    result["excursions"] = len(outside)
    if outside:
        result["worst_excursion"] = max(outside, key=abs)

    # Converging within the allowed window is a separate question from holding.
    result["converged_in_time"] = (
        result["converged_s"] is not None and result["converged_s"] <= converge_s
    )
    result["tolerance"] = tol
    result["converge_budget"] = converge_s
    return result


def fmt(value, spec=".1f"):
    return "--" if value is None else format(value, spec)


def report(path, dials, skip_first):
    rows = read_rows(path)
    if not rows:
        print(f"{path}: no rows")
        return
    print(f"\n{path}  ({len(rows)} ticks, {len(rows) * TICK_SECONDS:.0f}s)")
    for dial in dials:
        for start, target, samples in segments(rows, dial):
            if target == 0.0:
                continue  # dial not driven; the Actual is Baseline
            r = analyse_segment(dial, target, samples, skip_first)
            if r is None:
                continue
            verdict = "held" if r["excursions"] == 0 else "excursions"
            if r["converged_tick"] is None:
                verdict = "never converged"
            print(
                f"  {dial:<4} target {target:5.1f}  from tick {start:<4}"
                f" converge {fmt(r['converged_s'], '.0f'):>4}s"
                f" (budget {r['converge_budget']:.0f}s)"
                f"  error {fmt(r['mean_error'])} +/- {fmt(r['sd_error'])}"
                f"  (tol +/-{r['tolerance']:.0f})"
                f"  excursions {r['excursions']}"
                f" worst {fmt(r['worst_excursion'], '+.1f')}"
                f"  [{verdict}]"
            )
            if r["blank"]:
                print(
                    f"       {r['blank']} of {r['ticks']} ticks had no reading"
                    f" -- the instrument failed, it did not report idle"
                )
            if r["hard_zero"]:
                print(
                    f"       {r['hard_zero']} ticks read exactly 0 against a"
                    f" non-zero Target -- NVML answered, and answered zero"
                )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="CSV files, globs accepted")
    ap.add_argument("--dial", action="append", choices=sorted(TOLERANCE),
                    help="restrict to one dial; repeatable")
    ap.add_argument("--keep-first-tick", action="store_true",
                    help="include the startup tick that the brief discards")
    args = ap.parse_args()

    dials = args.dial or ["gpu", "vram", "cpu", "ram"]
    paths = []
    for pattern in args.paths:
        paths.extend(sorted(glob.glob(pattern)) or [pattern])

    for path in paths:
        try:
            report(path, dials, skip_first=not args.keep_first_tick)
        except OSError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
