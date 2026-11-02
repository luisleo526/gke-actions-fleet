#!/usr/bin/env python3
"""Sample runner PVC filesystem usage from kubelet summaries; never log pod specs or credentials."""

import argparse
import concurrent.futures
import datetime
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

PREFIX = "gke-gh-runner.io/"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def kube(context, *args):
    result = subprocess.run(
        ["kubectl", "--context", context, "--request-timeout=20s", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def volume_rows(summary, pods, timestamp):
    rows = []
    for pod in summary.get("pods", []):
        ref = pod["podRef"]
        metadata = pods.get(ref["uid"])
        if metadata is None:
            continue
        labels = metadata.get("labels", {})
        scale_set = labels.get("actions.github.com/scale-set-name") or ref["name"].rsplit("-runner-", 1)[0]
        for volume in pod.get("volume", []):
            if volume.get("name") != "work" or not all(k in volume for k in ("usedBytes", "capacityBytes")):
                continue
            rows.append(
                {
                    "type": "volume",
                    "timestamp": timestamp,
                    "stats_timestamp": volume.get("time"),
                    "namespace": ref["namespace"],
                    "target": ref["namespace"].removeprefix("arc-"),
                    "scale_set": scale_set,
                    "pod": ref["name"],
                    "pod_uid": ref["uid"],
                    "node": summary["node"]["nodeName"],
                    "pvc": volume.get("pvcRef", {}).get("name", ref["name"] + "-work"),
                    "used_bytes": volume["usedBytes"],
                    "capacity_bytes": volume["capacityBytes"],
                }
            )
    return rows


def sample(context):
    timestamp = utcnow()
    nodes = kube(context, "get", "nodes", "-l", PREFIX + "pool=runner", "-o", "json")["items"]
    pod_items = kube(context, "get", "pods", "-A", "-l", PREFIX + "workload=runner", "-o", "json")["items"]
    pods = {p["metadata"]["uid"]: p["metadata"] for p in pod_items}
    rows, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(kube, context, "get", "--raw", f"/api/v1/nodes/{n['metadata']['name']}/proxy/stats/summary"): n[
                "metadata"
            ]["name"]
            for n in nodes
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                rows.extend(volume_rows(future.result(), pods, timestamp))
            except (subprocess.SubprocessError, ValueError, KeyError, OSError) as error:
                errors.append({"node": futures[future], "error": type(error).__name__})
    rows.append(
        {"type": "sample", "timestamp": timestamp, "runner_nodes": len(nodes), "volumes": len(rows), "errors": errors}
    )
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exit-file", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--interval", type=float, default=120)
    args = parser.parse_args()
    if args.hours <= 0 or args.interval <= 0:
        parser.error("hours and interval must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.exit_file.parent.mkdir(parents=True, exist_ok=True)
    started, deadline = utcnow(), time.monotonic() + args.hours * 3600
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cycles, code = 0, 0
    try:
        with args.output.open("a", buffering=1) as output:
            while not stopping and time.monotonic() < deadline:
                cycle_start = time.monotonic()
                try:
                    rows = sample(args.context)
                except (subprocess.SubprocessError, ValueError, KeyError, OSError) as error:
                    rows = [{"type": "sample", "timestamp": utcnow(), "error": type(error).__name__}]
                for row in rows:
                    output.write(json.dumps(row, separators=(",", ":")) + "\n")
                output.flush()
                cycles += 1
                print(json.dumps({"cycle": cycles, **rows[-1]}), flush=True)
                next_sample = min(cycle_start + args.interval, deadline)
                while not stopping and time.monotonic() < next_sample:
                    time.sleep(min(1, next_sample - time.monotonic()))
    except Exception:
        code = 1
        raise
    finally:
        args.exit_file.write_text(
            json.dumps(
                {
                    "exit_code": code,
                    "started": started,
                    "finished": utcnow(),
                    "cycles": cycles,
                    "stopped_by_signal": stopping,
                }
            )
            + "\n"
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
