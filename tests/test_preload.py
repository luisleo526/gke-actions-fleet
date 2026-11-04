import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_runnerctl import ROOT, ctl

IMAGE = (
    "us-central1-docker.pkg.dev/your-gcp-project/gke-runner-images/"
    "engine-toolchain-bookworm@sha256:72fb3f2a4d3409e6050b17530585992784a21a4955b368cb8f8b392981abc780"
)
CARRIER = (
    "us-central1-docker.pkg.dev/your-gcp-project/gke-runner-images/"
    "engine-toolchain-bookworm-carrier@sha256:c0641444206b379132d7b37a816e5c8ffecd9d9b64e9216ee2508040a413ee32"
)
ARCHIVE = "/preload/engine-toolchain-bookworm.oci.tar"
ARCHIVE_SHA256 = "7d262df220abf67f1f10b9090616530017869c0e14564468ffa56bb53c45297f"
SCRIPT = (
    "set -eu\n"
    'echo "${PRELOAD_ARCHIVE_SHA256}  ${PRELOAD_ARCHIVE}" | sha256sum -c -\n'
    'docker load -i "${PRELOAD_ARCHIVE}"\n'
    'docker image inspect --format "preloaded id={{.Id}} platform={{.Os}}/{{.Architecture}} repodigests={{json .RepoDigests}}" "${PRELOAD_IMAGE}"\n'
    'test "$(docker image inspect --format "{{.Id}}" "${PRELOAD_IMAGE}")" = "${PRELOAD_IMAGE#*@}"\n'
    "docker images --digests --no-trunc"
)


class PreloadTests(unittest.TestCase):
    def setUp(self):
        self.c = ctl.load_config(ROOT / "tests/fixtures/protected-on-demand.json")
        self.profile = "gke-od-16cpu-32gb-amd64"
        self.p = self.c["profiles"][self.profile]
        self.item = self.p["preload_images"][0]

    def reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.c))
            return ctl.load_config(path)

    def reject(self):
        with self.assertRaises(ValueError):
            self.reload()

    def rendered(self, config):
        for target in config["targets"]:
            for profile, size in ctl.scale_set_variants(config, target).values():
                yield target, profile, size, ctl.scale_values(config, target, profile, size)

    def test_fixture_scopes_one_set(self):
        selected = []
        for target, _, _, values in self.rendered(self.c):
            names = [
                item["name"]
                for item in values["template"]["spec"]["initContainers"]
                if item["name"].startswith("preload-image-")
            ]
            if names:
                selected.append((target, values["runnerScaleSetName"], names))
        self.assertEqual(
            selected,
            [("pineforge-protected", "gkp-od-16cpu-32gb-amd64-128gb", ["preload-image-0"])],
        )

    def test_pod_contract(self):
        pod = ctl.scale_values(self.c, "pineforge-protected", self.profile, 128)["template"]["spec"]
        names = [item["name"] for item in pod["initContainers"]]
        self.assertGreater(names.index("preload-image-0"), names.index("dind"))
        dind = pod["initContainers"][names.index("dind")]
        self.assertEqual(dind["restartPolicy"], "Always")
        self.assertEqual(
            dind["startupProbe"],
            {"exec": {"command": ["docker", "info"]}, "periodSeconds": 5, "failureThreshold": 24},
        )
        preload = pod["initContainers"][names.index("preload-image-0")]
        self.assertEqual(
            preload,
            {
                "name": "preload-image-0",
                "image": CARRIER,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-euc", SCRIPT],
                "env": [
                    {"name": "DOCKER_HOST", "value": "unix:///var/run/docker.sock"},
                    {"name": "PRELOAD_IMAGE", "value": IMAGE},
                    {"name": "PRELOAD_ARCHIVE", "value": ARCHIVE},
                    {"name": "PRELOAD_ARCHIVE_SHA256", "value": ARCHIVE_SHA256},
                ],
                "securityContext": {
                    "runAsUser": 1001,
                    "runAsGroup": 123,
                    "runAsNonRoot": True,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {
                    "requests": {"cpu": "500m", "memory": "256Mi", "ephemeral-storage": "1Gi"},
                    "limits": {"cpu": "500m", "memory": "256Mi", "ephemeral-storage": "1Gi"},
                },
                "volumeMounts": [{"name": "dind-sock", "mountPath": "/var/run"}],
            },
        )
        self.assertNotIn("restartPolicy", preload)
        self.assertNotIn("envFrom", preload)
        self.assertTrue(all(set(env) == {"name", "value"} for env in preload["env"]))
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual({v["name"] for v in pod["volumes"]}, {"work", "dind-sock", "dind-externals"})
        self.assertFalse(any("secret" in v or "projected" in v for v in pod["volumes"]))

    def test_script_identity(self):
        self.assertEqual(ctl.PRELOAD_SCRIPT, SCRIPT)

    def test_extra_variants_stay_unselected(self):
        self.p["workspace_variants_gib"] = [64, 128, 256, 512]
        for size in (None, 64, 128, 256, 512):
            with self.subTest(size=size):
                pod = ctl.scale_values(self.c, "pineforge-protected", self.profile, size)["template"]["spec"]
                names = [item["name"] for item in pod["initContainers"] if item["name"].startswith("preload-image-")]
                self.assertEqual(names, ["preload-image-0"] if size == 128 else [])

    def test_absent_setting_keeps_values(self):
        config = copy.deepcopy(self.c)
        del config["profiles"][self.profile]["preload_images"]
        for target, profile, size, values in self.rendered(config):
            with self.subTest(target=target, profile=profile, size=size):
                self.assertFalse(
                    any(
                        item["name"].startswith("preload-image-")
                        for item in values["template"]["spec"]["initContainers"]
                    )
                )
                if (target, profile, size) != ("pineforge-protected", self.profile, 128):
                    self.assertEqual(values, ctl.scale_values(self.c, target, profile, size))

    def test_effective_scope_defaults(self):
        del self.p["targets"]
        del self.p["workspace_variants_gib"]
        self.item["targets"] = ["pineforge", "teamsync"]
        self.item["workspace_variants_gib"] = [64, 512]
        config = self.reload()
        selected = [
            (target, size)
            for target, profile, size, values in self.rendered(config)
            if profile == self.profile
            and any(item["name"].startswith("preload-image-") for item in values["template"]["spec"]["initContainers"])
        ]
        self.assertEqual(selected, [("teamsync", 64), ("teamsync", 512), ("pineforge", 64), ("pineforge", 512)])

    def test_reject_tagged_image(self):
        self.item["image"] = IMAGE.split("@")[0] + ":bookworm"
        self.reject()

    def test_reject_tag_and_digest(self):
        self.item["image"] = IMAGE.replace("@", ":bookworm@")
        self.reject()

    def test_reject_unpinned_carrier(self):
        self.item["carrier"] = CARRIER.split("@")[0]
        self.reject()

    def test_reject_uppercase_hash(self):
        self.item["archive_sha256"] = ARCHIVE_SHA256.upper()
        self.reject()

    def test_reject_short_hash(self):
        self.item["archive_sha256"] = ARCHIVE_SHA256[:-1]
        self.reject()

    def test_reject_relative_archive(self):
        self.item["archive"] = "preload/engine-toolchain-bookworm.oci.tar"
        self.reject()

    def test_reject_parent_archive(self):
        self.item["archive"] = "/preload/../engine-toolchain-bookworm.oci.tar"
        self.reject()

    def test_reject_outside_target(self):
        self.item["targets"] = ["pineforge"]
        self.reject()

    def test_reject_outside_variant(self):
        self.item["workspace_variants_gib"] = [64]
        self.reject()

    def test_reject_docker_false(self):
        self.p["docker"] = False
        self.reject()

    def test_reject_unknown_key(self):
        self.item["unknown"] = True
        self.reject()

    def test_reject_missing_key(self):
        for field in ("image", "carrier", "archive", "archive_sha256", "targets", "workspace_variants_gib"):
            with self.subTest(field=field):
                value = self.item.pop(field)
                self.reject()
                self.item[field] = value

    def test_reject_empty_preloads(self):
        self.p["preload_images"] = []
        self.reject()

    def test_reject_five_preloads(self):
        self.p["preload_images"] = [
            {**self.item, "image": IMAGE.replace("bookworm@", f"bookworm-{i}@")} for i in range(5)
        ]
        self.reject()

    def test_reject_duplicate_image(self):
        self.p["preload_images"].append(copy.deepcopy(self.item))
        self.reject()

    def test_reject_bad_preload_shapes(self):
        for value in (None, {}, "preload"):
            with self.subTest(value=value):
                self.p["preload_images"] = value
                self.reject()

    def test_reject_nonobject_entry(self):
        for value in (None, [], "preload"):
            with self.subTest(value=value):
                self.p["preload_images"] = [value]
                self.reject()

    def test_reject_invalid_targets(self):
        for value in ([], ["pineforge-protected"] * 2, [None], [{}], "pineforge-protected"):
            with self.subTest(value=value):
                self.item["targets"] = value
                self.reject()

    def test_reject_invalid_variants(self):
        for value in ([], [128, 128], [None], [True], ["128"], [{}], "128"):
            with self.subTest(value=value):
                self.item["workspace_variants_gib"] = value
                self.reject()

    def test_reject_invalid_references(self):
        for field in ("image", "carrier"):
            for value in (None, 123, "engine-toolchain@" + IMAGE.split("@")[1], IMAGE.upper(), IMAGE[:-1]):
                with self.subTest(field=field, value=value):
                    self.item[field] = value
                    self.reject()
            self.item[field] = IMAGE if field == "image" else CARRIER

    def test_reject_invalid_archives(self):
        for value in (None, 123, "/preload/archive name.tar", "/preload/archive;false"):
            with self.subTest(value=value):
                self.item["archive"] = value
                self.reject()

    def test_reject_invalid_hashes(self):
        for value in (None, 123, ARCHIVE_SHA256 + "0", "g" * 64):
            with self.subTest(value=value):
                self.item["archive_sha256"] = value
                self.reject()

    def test_four_preloads_keep_indices(self):
        self.p["workspace_variants_gib"] = [64, 128]
        self.p["preload_images"] = [
            {
                **self.item,
                "image": IMAGE.replace("bookworm@", f"bookworm-{i}@"),
                "workspace_variants_gib": [64 if i == 0 else 128],
            }
            for i in range(4)
        ]
        config = self.reload()
        pod = ctl.scale_values(config, "pineforge-protected", self.profile, 128)["template"]["spec"]
        names = [item["name"] for item in pod["initContainers"]]
        self.assertEqual(
            names,
            ["init-work", "init-dind-externals", "dind", "preload-image-1", "preload-image-2", "preload-image-3"],
        )


if __name__ == "__main__":
    unittest.main()
