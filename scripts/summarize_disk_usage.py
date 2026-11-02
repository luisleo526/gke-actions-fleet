#!/usr/bin/env python3
"""Per-target/scale-set percentiles of each observed runner pod's peak PVC usage."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def summarize(lines):
    peaks = defaultdict(dict)
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # A concurrently appended final line may be incomplete.
        if row.get("type") != "volume":
            continue
        key, job = (row["target"], row["scale_set"]), row["pod_uid"]
        peaks[key][job] = max(peaks[key].get(job, 0), row["used_bytes"])
    result = []
    for (target, scale_set), jobs in sorted(peaks.items()):
        sizes = sorted(value / 2**30 for value in jobs.values())
        result.append(
            {
                "target": target,
                "scale_set": scale_set,
                "jobs": len(sizes),
                "p50_gib": sizes[math.ceil(len(sizes) * 0.5) - 1],
                "p95_gib": sizes[math.ceil(len(sizes) * 0.95) - 1],
                "max_gib": sizes[-1],
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path)
    args = parser.parse_args()
    with args.jsonl.open() as source:
        result = summarize(source)
    print("Jobs = distinct observed runner pods; percentiles use each pod's sampled peak (nearest rank).")
    print("target\tscale_set\tjobs\tp50 GiB\tp95 GiB\tmax GiB")
    for row in result:
        print(
            f"{row['target']}\t{row['scale_set']}\t{row['jobs']}\t{row['p50_gib']:.3f}\t{row['p95_gib']:.3f}\t{row['max_gib']:.3f}"
        )


if __name__ == "__main__":
    main()
