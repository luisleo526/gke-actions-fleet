import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_runnerctl import ROOT, ctl


class ProfileTargetTests(unittest.TestCase):
    def setUp(self):
        self.c = ctl.load_config(ROOT / "tests/fixtures/protected-on-demand.json")
        self.od = "gke-od-32cpu-64gb-amd64"
        self.legacy = "gke-2cpu-4gb-amd64"

    def reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.c))
            return ctl.load_config(path)

    def test_omitted_targets_keep_all_targets_and_global_disk_variants(self):
        for target in self.c["targets"]:
            variants = ctl.scale_set_variants(self.c, target)
            self.assertEqual({size for p, size in variants.values() if p == self.legacy}, {None, 64, 128, 256, 512})
            od = [(p, size) for p, size in variants.values() if p.startswith("gke-od-")]
            self.assertEqual(len(od), 8 if target.endswith("-protected") else 0)
            self.assertTrue(all(size in (None, 128) for _, size in od))

    def test_target_names_and_variant_overrides_are_validated(self):
        original = copy.deepcopy(self.c)
        for targets in (["unknown"], [], "teamsync-protected", [None], [{}], ["teamsync", "teamsync"]):
            with self.subTest(targets=targets):
                self.c = copy.deepcopy(original)
                self.c["profiles"][self.od]["targets"] = targets
                with self.assertRaises(ValueError):
                    self.reload()
        for sizes in ([32], [128, 128], [True], ["128"], None, "128"):
            with self.subTest(sizes=sizes):
                self.c = copy.deepcopy(original)
                self.c["profiles"][self.od]["workspace_variants_gib"] = sizes
                with self.assertRaises(ValueError):
                    self.reload()
        self.c = copy.deepcopy(original)
        self.c["profiles"][self.od]["workspace_variants_gib"] = []
        self.reload()
        self.assertEqual([s for p, s in ctl.scale_set_variants(self.c).values() if p == self.od], [None])

    def test_render_and_readback_are_target_scoped_without_changing_pool_inputs(self):
        before = ctl.node_pool_variants(self.c)
        with tempfile.TemporaryDirectory() as directory:
            ctl.render(self.c, directory)
            tf = json.loads((Path(directory) / "terraform.tfvars.json").read_text())
            self.assertEqual(tf["profiles"], before)
            for target in self.c["targets"]:
                values = []
                for file in (Path(directory) / target).glob("gke-*.json"):
                    value = json.loads(file.read_text())
                    values.append({"metadata": {"name": file.stem}, "spec": value})
                    self.assertLessEqual(len(value["runnerScaleSetName"]), 45)
                self.assertEqual(len(values), 13 if target.endswith("-protected") else 5)
                ctl.validate_scale_sets(self.c, target, values)
                if not target.endswith("-protected"):
                    values.append({"metadata": {"name": self.od}, "spec": {"runnerScaleSetName": self.od}})
                    with self.assertRaises(ValueError):
                        ctl.validate_scale_sets(self.c, target, values)
        for p in self.c["profiles"].values():
            p.pop("targets", None)
            p.pop("workspace_variants_gib", None)
        self.assertEqual(ctl.node_pool_variants(self.c), before)

    def test_on_demand_profiles_have_dedicated_bounded_pools_in_two_zones(self):
        pools = ctl.node_pool_variants(self.c)
        for profile, settings in self.c["profiles"].items():
            if not profile.startswith("gke-od-"):
                continue
            candidates = [p for p in pools.values() if p["profile_name"] == profile]
            self.assertEqual(len(candidates), 2)
            self.assertEqual({p["zone"] for p in candidates}, {"us-central1-a", "us-central1-b"})
            self.assertTrue(all(p["spot"] is False and p["architecture"] == "amd64" for p in candidates))
            self.assertEqual(sum(p["max_runners"] for p in candidates), settings["max_runners"])
            value = ctl.scale_values(self.c, "teamsync-protected", profile, 128)
            self.assertEqual(value["runnerGroup"], "gke-protected")
            self.assertEqual(value["minRunners"], 0)
            self.assertEqual(value["template"]["spec"]["nodeSelector"][ctl.PREFIX + "profile"], profile)
            self.assertEqual(value["runnerScaleSetName"], profile.replace("gke-", "gkp-", 1) + "-128gb")

    def test_additions_and_regular_install_never_install_on_excluded_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            ctl.render(self.c, directory)
            charts = {"gha-runner-scale-set": "pinned.tgz", "gha-runner-scale-set-controller": "controller.tgz"}
            with patch.object(ctl, "kube"), patch.object(ctl, "run") as run:
                # Existing legacy releases include drift: preserve all supplied values.
                def response(args, *unused, **kwargs):
                    if args[:2] == ["helm", "list"]:
                        return json.dumps(
                            [
                                {"name": n, "status": "deployed"}
                                for n in ctl.scale_set_variants(
                                    {**self.c, "profiles": {self.legacy: self.c["profiles"][self.legacy]}}
                                )
                            ]
                        )
                    if args[:3] == ["helm", "get", "values"]:
                        return "{}"

                run.side_effect = response
                ctl.install_additions(self.c, directory, charts, plan_only=True)
                plan = json.loads((Path(directory) / "additions.json").read_text())
                self.assertEqual(len(plan), 16)
                self.assertEqual({p["namespace"] for p in plan}, {"arc-teamsync-protected", "arc-pineforge-protected"})
                ctl.install_additions(self.c, directory, charts)
                installs = [call.args[0] for call in run.call_args_list if call.args[0][:2] == ["helm", "install"]]
                self.assertEqual(len(installs), 16)
                self.assertTrue(all(a[a.index("--namespace") + 1].endswith("-protected") for a in installs))
                self.assertFalse(any(a.args[0][:2] == ["helm", "upgrade"] for a in run.call_args_list))
            with (
                patch.object(ctl, "kube"),
                patch.object(ctl, "run") as run,
                patch.object(ctl, "fetch_charts", return_value=charts),
            ):
                ctl.install(self.c, directory)
                calls = [
                    a.args[0]
                    for a in run.call_args_list
                    if a.args[0][:2] == ["helm", "upgrade"] and a.args[0][3].startswith("gke-od-")
                ]
                self.assertEqual(len(calls), 16)
                self.assertTrue(all(a[a.index("--namespace") + 1].endswith("-protected") for a in calls))


if __name__ == "__main__":
    unittest.main()
