import copy
import importlib.util
import json
import os
import shlex
import stat
import subprocess
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
        for architecture in ["amd64", "arm64"]:
            limits = (
                {2: 16, 4: 16, 8: 16, 16: 16, 32: 16} if architecture == "amd64" else {2: 16, 4: 16, 8: 8, 16: 8, 32: 8}
            )
            profiles = [p for p in self.c["profiles"].values() if p["architecture"] == architecture]
            self.assertEqual({p["cpu"]: p["max_runners"] for p in profiles}, limits)
            self.assertEqual(sum(p["max_runners"] for p in profiles), 80 if architecture == "amd64" else 56)

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

    def test_all_job_data_share_one_disposable_configured_disk(self):
        pod = ctl.scale_values(self.c, "example", "gke-2cpu-4gb-arm64")["template"]["spec"]
        volume = pod["volumes"][0]["ephemeral"]["volumeClaimTemplate"]["spec"]
        self.assertEqual(volume["resources"]["requests"]["storage"], "480Gi")
        self.assertEqual(volume["storageClassName"], "runner-work")
        docker = next(c for c in pod["initContainers"] if c["name"] == "dind")
        self.assertEqual(next(m for m in docker["volumeMounts"] if m["mountPath"] == "/var/lib/docker")["name"], "work")
        self.assertEqual(ctl.storage_class(self.c)["reclaimPolicy"], "Delete")
        self.assertEqual(ctl.storage_class(self.c)["volumeBindingMode"], "WaitForFirstConsumer")
        self.assertTrue(ctl.storage_class(self.c)["allowVolumeExpansion"])

    def test_workspace_size_can_be_overridden_globally_and_per_profile(self):
        self.c["workspace_gib"] = 240
        self.c["profiles"]["gke-2cpu-4gb-amd64"]["workspace_gib"] = 640
        for profile, expected in [("gke-2cpu-4gb-amd64", "640Gi"), ("gke-2cpu-4gb-arm64", "240Gi")]:
            pod = ctl.scale_values(self.c, "example", profile)["template"]["spec"]
            self.assertEqual(
                pod["volumes"][0]["ephemeral"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"],
                expected,
            )

    def test_standard_tool_cache_uses_the_job_disk_in_runner_and_docker(self):
        for name in self.c["profiles"]:
            with self.subTest(profile=name):
                pod = ctl.scale_values(self.c, "example", name)["template"]["spec"]
                runner = pod["containers"][0]
                env = {entry["name"]: entry.get("value") for entry in runner["env"]}
                self.assertEqual(env["RUNNER_TOOL_CACHE"], "/opt/hostedtoolcache")
                self.assertEqual(env["AGENT_TOOLSDIRECTORY"], "/opt/hostedtoolcache")
                docker = next(item for item in pod["initContainers"] if item["name"] == "dind")
                for container in [runner, docker]:
                    mount = next(m for m in container["volumeMounts"] if m["mountPath"] == "/opt/hostedtoolcache")
                    self.assertEqual(
                        mount, {"name": "work", "mountPath": "/opt/hostedtoolcache", "subPath": "toolcache"}
                    )
                self.assertEqual(
                    pod["volumes"][0]["ephemeral"]["volumeClaimTemplate"]["spec"]["storageClassName"], "runner-work"
                )

    def test_work_initializer_clears_inherited_setgid_and_preserves_other_volumes(self):
        pod = ctl.scale_values(self.c, "example", "gke-2cpu-4gb-amd64")["template"]["spec"]
        self.assertEqual(pod["securityContext"]["fsGroup"], 123)
        self.assertEqual(pod["initContainers"][0]["volumeMounts"], [{"name": "work", "mountPath": "/mnt/work"}])
        docker = next(item for item in pod["initContainers"] if item["name"] == "dind")
        self.assertIn("--group=123", docker["args"])
        for container in [pod["containers"][0], docker]:
            self.assertIn({"name": "dind-sock", "mountPath": "/var/run"}, container["volumeMounts"])
        command = pod["initContainers"][0]["command"][-1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            work = base / "job volume"
            for name in ["", "lost+found"]:
                path = work / name
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o2770)
            socket_volume = base / "socket-volume"
            socket_volume.mkdir(mode=0o2770)
            socket_volume.chmod(0o2770)
            before_socket = socket_volume.stat()
            # Ownership needs root in the real pod. Exercise mkdir/chmod/find on
            # the real filesystem while replacing only that privileged chown.
            binary = base / "bin"
            binary.mkdir()
            (binary / "chown").write_text("#!/bin/sh\nexit 0\n")
            (binary / "chown").chmod(0o755)
            subprocess.run(
                ["/bin/bash", "-ec", "umask 022; " + command.replace("/mnt/work", shlex.quote(str(work)))],
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]},
                check=True,
                capture_output=True,
            )
            for path in [work, *work.rglob("*")]:
                if path.is_dir():
                    self.assertFalse(path.stat().st_mode & stat.S_ISGID, str(path))
            for name in ["workspace", "workspace/_temp", "toolcache"]:
                self.assertEqual((work / name).stat().st_mode & 0o7777, 0o770)
            private = work / "workspace/_temp/private"
            private.mkdir(mode=0o700)
            self.assertEqual(private.stat().st_mode & 0o7777, 0o700)
            self.assertEqual(socket_volume.stat().st_mode, before_socket.st_mode)
            self.assertEqual(socket_volume.stat().st_gid, before_socket.st_gid)

    def test_workspace_size_rejects_invalid_capacity(self):
        for value in [0, -1, True, "480", 16385]:
            with self.subTest(value=value):
                self.config_error(lambda c: c.update(workspace_gib=value))

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
                self.assertEqual(runner["resources"]["limits"]["ephemeral-storage"], "5Gi")
                self.assertEqual(runner["resources"]["requests"]["ephemeral-storage"], "2Gi")
                # Kubelet's pod cap is the sum of container limits, sidecar included.
                sidecars = [c for c in pod["initContainers"] if c.get("restartPolicy") == "Always"]
                cap = sum(
                    int(c["resources"]["limits"]["ephemeral-storage"].removesuffix("Gi"))
                    for c in pod["containers"] + sidecars
                )
                self.assertEqual(cap, 7 if sidecars else 5)
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

    def test_target_image_overrides_global_for_runner_and_init_containers(self):
        self.c["runner_image"] = "ghcr.io/example/global@sha256:" + "a" * 64
        image = "us-central1-docker.pkg.dev/example-project/runner-images/toolchain@sha256:" + "b" * 64
        self.c["targets"]["example"]["runner_image"] = image
        pod = ctl.scale_values(self.c, "example", "gke-2cpu-4gb-amd64")["template"]["spec"]
        for container in [pod["containers"][0], *pod["initContainers"][:2]]:
            self.assertEqual(container["image"], image)
        self.assertEqual(next(c for c in pod["initContainers"] if c["name"] == "dind")["image"], ctl.DOCKER_IMAGE)

    def test_same_org_protected_target_has_distinct_names_and_shared_pool_selector(self):
        self.c["targets"]["protected"] = {
            **self.c["targets"]["example"],
            "runner_group": "protected",
            "scale_set_prefix": "gkp-",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.c))
            parsed = ctl.load_config(path)
        values = ctl.scale_values(parsed, "protected", "gke-8cpu-16gb-amd64")
        self.assertEqual(values["runnerScaleSetName"], "gkp-8cpu-16gb-amd64")
        self.assertEqual(values["runnerGroup"], "protected")
        self.assertEqual(values["template"]["spec"]["nodeSelector"][ctl.PREFIX + "profile"], "gke-8cpu-16gb-amd64")
        self.assertNotEqual(ctl.namespace("protected"), ctl.namespace("example"))

    def test_same_org_overlapping_names_are_rejected_even_in_different_groups(self):
        self.config_error(
            lambda c: c["targets"].update(protected={**c["targets"]["example"], "runner_group": "protected"})
        )

    def test_prefix_cannot_recreate_existing_same_org_names(self):
        self.config_error(
            lambda c: c["targets"].update(
                protected={**c["targets"]["example"], "runner_group": "protected", "scale_set_prefix": "gke-"}
            )
        )

    def test_invalid_target_image_or_prefix_is_rejected(self):
        original = copy.deepcopy(self.c)
        for change in [
            {"runner_image": "ghcr.io/example/image:latest"},
            {"scale_set_prefix": "BAD!"},
            {"scale_set_prefix": "x" * 46},
        ]:
            self.c = copy.deepcopy(original)
            self.config_error(lambda c: c["targets"]["example"].update(change))

    def test_readback_compares_github_scale_set_names(self):
        self.c["targets"]["example"]["scale_set_prefix"] = "gkp-"
        sets = [
            {
                "metadata": {"name": "resource-" + str(i)},
                "spec": {"runnerScaleSetName": ctl.scale_set_name(self.c["targets"]["example"], profile)},
            }
            for i, profile in enumerate(self.c["profiles"])
        ]
        ctl.validate_scale_sets(self.c, "example", sets)
        sets[0]["spec"]["runnerScaleSetName"] = "wrong-name"
        with self.assertRaises(ValueError):
            ctl.validate_scale_sets(self.c, "example", sets)

    def test_repository_scoped_image_readers_render_without_image_credentials(self):
        self.c["artifact_registry_repositories"] = [
            {"project_id": "example-project", "location": "us-central1", "repository": "runner-images"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            ctl.render(self.c, directory)
            variables = json.loads((Path(directory) / "terraform.tfvars.json").read_text())
        self.assertEqual(variables["artifact_registry_repositories"], self.c["artifact_registry_repositories"])
        self.assertNotIn("runner_image", variables)

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
