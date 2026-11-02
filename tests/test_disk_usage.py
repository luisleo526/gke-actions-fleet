import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


sampler = module("sample_disk_usage")
summary = module("summarize_disk_usage")


class DiskUsageTests(unittest.TestCase):
    def test_summary_counts_pods_once_and_uses_per_job_peaks_not_sample_weights(self):
        rows = []
        for uid, usage in [("one", 1), ("one", 8), ("one", 3), ("two", 4), ("three", 16)]:
            rows.append(
                json.dumps(
                    {
                        "type": "volume",
                        "target": "example",
                        "scale_set": "gke-2cpu-4gb-amd64-64gb",
                        "pod_uid": uid,
                        "used_bytes": usage * 2**30,
                    }
                )
            )
        rows += [json.dumps({"type": "sample"}), '{"partial']
        self.assertEqual(
            summary.summarize(rows),
            [
                {
                    "target": "example",
                    "scale_set": "gke-2cpu-4gb-amd64-64gb",
                    "jobs": 3,
                    "p50_gib": 8,
                    "p95_gib": 16,
                    "max_gib": 16,
                }
            ],
        )

    def test_sampler_records_only_selected_runner_work_pvc_and_no_environment(self):
        pod = {
            "podRef": {"uid": "uid", "namespace": "arc-example", "name": "gke-2cpu-4gb-amd64-64gb-runner-test"},
            "volume": [
                {"name": "work", "usedBytes": 10, "capacityBytes": 64 * 2**30, "pvcRef": {"name": "job-work"}},
                {"name": "socket", "usedBytes": 100, "capacityBytes": 1000},
            ],
        }
        document = {"node": {"nodeName": "runner-node"}, "pods": [pod]}
        self.assertEqual(sampler.volume_rows(document, {}, "now"), [])
        rows = sampler.volume_rows(
            document, {"uid": {"labels": {"actions.github.com/scale-set-name": "exact-label"}}}, "now"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "example")
        self.assertEqual(rows[0]["scale_set"], "exact-label")
        self.assertEqual(rows[0]["pvc"], "job-work")
        self.assertEqual(rows[0]["capacity_bytes"], 64 * 2**30)
