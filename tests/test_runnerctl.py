import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runnerctl", ROOT / "scripts/runnerctl.py")
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.c = ctl.load_config(ROOT / "config.example.json")

    def config_error(self, mutate):
        mutate(self.c)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.c))
            with self.assertRaises(ValueError):
                ctl.load_config(path)

    def test_requested_sizes_on_both_architectures(self):
        actual = {(p["architecture"], p["cpu"], p["memory_gib"]) for p in self.c["profiles"].values()}
        self.assertEqual(
            actual,
            {(a, cpu, ram) for a in ("amd64", "arm64") for cpu, ram in [(2, 4), (4, 8), (8, 16), (16, 32), (32, 64)]},
        )

    def test_capacity_limits_are_independent_for_each_architecture(self):
        limits = {2: 16, 4: 16, 8: 8, 16: 8, 32: 8}
        for architecture in ["amd64", "arm64"]:
            profiles = [p for p in self.c["profiles"].values() if p["architecture"] == architecture]
            self.assertEqual({p["cpu"]: p["max_runners"] for p in profiles}, limits)
            self.assertEqual(sum(p["max_runners"] for p in profiles), 56)

    def test_machine_variants_share_one_profile_capacity_budget(self):
        pools = ctl.node_pool_variants(self.c)
        self.assertEqual(len(pools), 20)
        for profile_name, profile in self.c["profiles"].items():
            candidates = [p for p in pools.values() if p["profile_name"] == profile_name]
            self.assertEqual(len(candidates), 2)
            self.assertEqual(sum(p["max_runners"] for p in candidates), profile["max_runners"])
            self.assertEqual({p["architecture"] for p in candidates}, {profile["architecture"]})
        self.assertEqual(pools["gke-32cpu-64gb-arm64-alt1"]["zone"], "us-central1-b")

    def test_wrong_architecture_in_alternative_is_rejected(self):
        self.config_error(
            lambda c: c["profiles"]["gke-2cpu-4gb-amd64"]["alternatives"][0].update(machine_type="t2a-standard-8")
        )

    def test_alternative_cannot_multiply_profile_capacity(self):
        self.config_error(lambda c: c["profiles"]["gke-2cpu-4gb-amd64"]["alternatives"][0].update(max_nodes=16))

    def test_alternative_zone_must_be_in_cluster_region(self):
        self.config_error(lambda c: c["profiles"]["gke-2cpu-4gb-amd64"]["alternatives"][0].update(zone="asia-east1-a"))

    def test_wrong_architecture_rejected_before_deployment(self):
        self.config_error(lambda c: c["profiles"]["gke-2cpu-4gb-arm64"].update(architecture="amd64"))

    def test_insufficient_node_memory_rejected(self):
        self.config_error(lambda c: c["profiles"]["gke-32cpu-64gb-amd64"].update(machine_type="n2-custom-36-65536"))

    def test_invalid_custom_machine_rejected(self):
        self.config_error(lambda c: c["profiles"]["gke-32cpu-64gb-amd64"].update(machine_type="n2-custom-34-77824"))

    def test_unknown_setting_rejected(self):
        self.config_error(lambda c: c.update(min_runner=3))

    def test_no_open_api_endpoint(self):
        self.config_error(lambda c: c.update(admin_cidrs=["0.0.0.0/0"]))

    def test_repository_group_is_rejected(self):
        self.config_error(lambda c: c["targets"]["example"].update(github_url="https://github.com/org/repo"))

    def test_t2a_spot_rejected(self):
        self.config_error(lambda c: c["profiles"]["gke-2cpu-4gb-arm64"].update(spot=True))

    def test_pods_cannot_fall_back_to_wrong_node_or_control_plane(self):
        for name, profile in self.c["profiles"].items():
            with self.subTest(profile=name):
                values = ctl.scale_values(self.c, "example", name)
                pod = values["template"]["spec"]
                self.assertEqual(
                    pod["nodeSelector"], {ctl.PREFIX + "profile": name, "kubernetes.io/arch": profile["architecture"]}
                )
                self.assertEqual(values["minRunners"], 0)
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["containers"][0]["resources"]["limits"]["cpu"], str(profile["cpu"]))
                self.assertEqual(pod["containers"][0]["resources"]["limits"]["memory"], f"{profile['memory_gib']}Gi")
                docker = next(item for item in pod["initContainers"] if item["name"] == "dind")
                self.assertEqual(docker["resources"]["limits"]["cpu"], str(profile["cpu"]))
                self.assertEqual(docker["resources"]["limits"]["memory"], f"{profile['memory_gib']}Gi")
                self.assertEqual(values["listenerTemplate"]["spec"]["nodeSelector"][ctl.PREFIX + "pool"], "system")
                self.assertEqual(
                    pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0][
                        "namespaceSelector"
                    ],
                    {},
                )

    def test_all_job_data_share_one_disposable_80gib_disk(self):
        pod = ctl.scale_values(self.c, "example", "gke-2cpu-4gb-arm64")["template"]["spec"]
        volume = pod["volumes"][0]["ephemeral"]["volumeClaimTemplate"]["spec"]
        self.assertEqual(volume["resources"]["requests"]["storage"], "80Gi")
        self.assertEqual(volume["storageClassName"], "runner-work")
        docker = next(c for c in pod["initContainers"] if c["name"] == "dind")
        self.assertEqual(next(m for m in docker["volumeMounts"] if m["mountPath"] == "/var/lib/docker")["name"], "work")
        self.assertEqual(ctl.storage_class(self.c)["reclaimPolicy"], "Delete")
        self.assertEqual(ctl.storage_class(self.c)["volumeBindingMode"], "WaitForFirstConsumer")

    def test_no_docker_profile_has_no_privileged_sidecar(self):
        self.c["profiles"]["gke-2cpu-4gb-arm64"]["docker"] = False
        pod = ctl.scale_values(self.c, "example", "gke-2cpu-4gb-arm64")["template"]["spec"]
        self.assertEqual([c["name"] for c in pod["initContainers"]], ["init-work"])
        self.assertFalse(any(e["name"] == "DOCKER_HOST" for e in pod["containers"][0]["env"]))

    def test_standard_runner_can_use_sudo_without_host_privileges(self):
        for name in self.c["profiles"]:
            with self.subTest(profile=name):
                pod = ctl.scale_values(self.c, "example", name)["template"]["spec"]
                runner = pod["containers"][0]
                self.assertTrue(runner["securityContext"]["runAsNonRoot"])
                self.assertTrue(runner["securityContext"]["allowPrivilegeEscalation"])
                self.assertIn(27, pod["securityContext"]["supplementalGroups"])
                self.assertNotIn("capabilities", runner["securityContext"])
                self.assertNotIn("privileged", runner["securityContext"])
                self.assertNotIn("ephemeral-storage", runner["resources"]["limits"])
                self.assertEqual(runner["resources"]["requests"]["ephemeral-storage"], "2Gi")
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertFalse(pod.get("hostPID", False))
                self.assertFalse(pod.get("hostNetwork", False))

    def test_sudo_can_be_disabled_for_restricted_profiles(self):
        self.c["profiles"]["gke-2cpu-4gb-amd64"]["allow_sudo"] = False
        pod = ctl.scale_values(self.c, "example", "gke-2cpu-4gb-amd64")["template"]["spec"]
        self.assertFalse(pod["containers"][0]["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(pod["containers"][0]["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertNotIn("supplementalGroups", pod["securityContext"])

    def test_sudo_setting_rejects_non_boolean_values(self):
        self.config_error(lambda c: c["profiles"]["gke-2cpu-4gb-amd64"].update(allow_sudo="true"))

    def test_custom_image_applies_to_all_runner_init_containers(self):
        image = "ghcr.io/example/runner@sha256:" + "a" * 64
        self.c["runner_image"] = image
        pod = ctl.scale_values(self.c, "example", "gke-2cpu-4gb-arm64")["template"]["spec"]
        for c in [pod["containers"][0], *pod["initContainers"][:2]]:
            self.assertEqual(c["image"], image)

    def test_render_contains_no_credentials_and_separates_targets(self):
        self.c["targets"]["second"] = {"github_url": "https://github.com/another/repo", "github_secret": "another-app"}
        with tempfile.TemporaryDirectory() as directory:
            ctl.render(self.c, directory)
            files = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(files), 27)
            for f in files:
                self.assertNotIn("github_app_private_key", f.read_text())
            second = json.loads((Path(directory) / "second/gke-2cpu-4gb-arm64.json").read_text())
            self.assertEqual(second["githubConfigSecret"], "another-app")
            self.assertNotIn("runnerGroup", second)

    def test_kubectl_always_targets_configured_cluster(self):
        with patch.object(ctl, "run") as run:
            ctl.kube(self.c, ["get", "pods"])
        self.assertEqual(
            run.call_args.args[0][:3], ["kubectl", "--context", "gke_your-gcp-project_us-central1-a_github-runners"]
        )

    def test_idle_requires_vm_disk_and_kubernetes_cleanup(self):
        idle = {
            "nodes": [],
            "volumes": [],
            "disks": [],
            "targets": {"example": {"pods": [], "runners": [], "claims": []}},
            "instance_groups": {"group": {"target_size": 0, "instances": [], "current_actions": {}}},
        }
        self.assertTrue(ctl.is_idle(idle))
        for key in ["nodes", "volumes", "disks"]:
            state = copy.deepcopy(idle)
            state[key] = [{"name": "remaining"}]
            self.assertFalse(ctl.is_idle(state))
        for key in ["pods", "runners", "claims"]:
            state = copy.deepcopy(idle)
            state["targets"]["example"][key] = [{"name": "remaining"}]
            self.assertFalse(ctl.is_idle(state))
        state = copy.deepcopy(idle)
        state["instance_groups"]["group"]["instances"] = [{"instance": "still-deleting"}]
        self.assertFalse(ctl.is_idle(state))
        state = copy.deepcopy(idle)
        state["instance_groups"]["group"]["target_size"] = 1
        self.assertFalse(ctl.is_idle(state))
        state = copy.deepcopy(idle)
        state["instance_groups"]["group"]["current_actions"] = {"creating": 1}
        self.assertFalse(ctl.is_idle(state))
        state["instance_groups"] = {}
        self.assertFalse(ctl.is_idle(state))

    def test_network_isolation_includes_metadata_and_private_ranges(self):
        policy = ctl.network_policy("example")["spec"]
        self.assertEqual(policy["ingress"], [])
        blocked = policy["egress"][1]["to"][0]["ipBlock"]["except"]
        self.assertIn("169.254.0.0/16", blocked)
        self.assertIn("10.0.0.0/8", blocked)
        dns = policy["egress"][0]
        self.assertIn({"ipBlock": {"cidr": "10.45.0.10/32"}}, dns["to"])
        self.assertEqual({p["port"] for p in dns["ports"]}, {53})

    def test_gke_group_and_manager_urls_resolve_to_the_same_managed_group(self):
        prefix = "https://www.googleapis.com/compute/v1/projects/test-project/zones/us-central1-a/"
        for resource in ["instanceGroups", "instanceGroupManagers"]:
            self.assertEqual(
                ctl.parse_group_url(prefix + resource + "/test-group"), ("test-project", "us-central1-a", "test-group")
            )
        with self.assertRaises(ValueError):
            ctl.parse_group_url("https://example.com/test-group")


if __name__ == "__main__":
    unittest.main()
