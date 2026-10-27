#!/usr/bin/env python3
"""Render every profile using checksum-pinned official charts and validate ARC CRDs."""

import argparse
import json
import tarfile
from pathlib import Path

import jsonschema
import runnerctl as ctl
import yaml


def verify(c, out):
    ctl.render(c, out)
    charts = ctl.fetch_charts(out)
    schemas = {}
    with tarfile.open(charts["gha-runner-scale-set-controller"]) as archive:
        for member in archive.getmembers():
            if "/crds/" not in member.name or not member.name.endswith(".yaml"):
                continue
            for document in yaml.safe_load_all(archive.extractfile(member)):
                if document and document.get("kind") == "CustomResourceDefinition":
                    for version in document["spec"]["versions"]:
                        schemas[(document["spec"]["names"]["kind"], version["name"])] = version["schema"][
                            "openAPIV3Schema"
                        ]
    count = 0
    for version in ["1.32.0", "1.35.0"]:
        ctl.run(
            [
                "helm",
                "template",
                "arc-controller",
                charts["gha-runner-scale-set-controller"],
                "--namespace",
                "arc-system",
                "--kube-version",
                version,
                "-f",
                str(Path(out) / "controller.json"),
            ],
            capture=True,
        )
        for target in c["targets"]:
            for name, profile in c["profiles"].items():
                rendered = ctl.run(
                    [
                        "helm",
                        "template",
                        name,
                        charts["gha-runner-scale-set"],
                        "--namespace",
                        ctl.namespace(target),
                        "--kube-version",
                        version,
                        "-f",
                        str(Path(out) / target / f"{name}.json"),
                    ],
                    capture=True,
                )
                documents = list(yaml.safe_load_all(rendered))
                scale = next(d for d in documents if d and d.get("kind") == "AutoscalingRunnerSet")
                jsonschema.validate(scale, schemas[("AutoscalingRunnerSet", "v1alpha1")])
                pod = scale["spec"]["template"]["spec"]
                ctl.require(
                    scale["spec"]["runnerScaleSetName"] == ctl.scale_set_name(c["targets"][target], name),
                    "Target scale-set name lost",
                )
                expected_image = c["targets"][target].get("runner_image", c.get("runner_image", ctl.RUNNER_IMAGE))
                ctl.require(pod["containers"][0]["image"] == expected_image, "Target runner image lost")
                runner = pod["containers"][0]
                env = {entry["name"]: entry.get("value") for entry in runner["env"]}
                for key in ["RUNNER_TOOL_CACHE", "AGENT_TOOLSDIRECTORY"]:
                    ctl.require(env[key] == "/opt/hostedtoolcache", "Standard tool-cache path lost")
                expected_mount = {"name": "work", "mountPath": "/opt/hostedtoolcache", "subPath": "toolcache"}
                ctl.require(expected_mount in runner["volumeMounts"], "Tool cache is not on the job disk")
                ctl.require(
                    pod["nodeSelector"]["kubernetes.io/arch"] == profile["architecture"], "Architecture selector lost"
                )
                ctl.require(
                    pod["volumes"][0]["ephemeral"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"]
                    == f"{profile.get('workspace_gib', c.get('workspace_gib', ctl.DEFAULT_WORKSPACE_GIB))}Gi",
                    "Work volume lost",
                )
                if profile["docker"]:
                    docker = next(i for i in pod["initContainers"] if i["name"] == "dind")
                    ctl.require(expected_mount in docker["volumeMounts"], "Docker tool-cache mount lost")
                    ctl.require(
                        next(i for i in pod["initContainers"] if i["name"] == "dind")["restartPolicy"] == "Always",
                        "Native sidecar lost",
                    )
                output = Path(out) / "rendered" / version / target / f"{name}.yaml"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(rendered)
                count += 1
    receipt = {
        "chart_version": ctl.ARC_VERSION,
        "chart_sha256": ctl.CHART_HASHES,
        "validated_scale_sets": count,
        "runner_image": c.get("runner_image", ctl.RUNNER_IMAGE),
        "target_images": {
            target: t.get("runner_image", c.get("runner_image", ctl.RUNNER_IMAGE)) for target, t in c["targets"].items()
        },
        "docker_image": ctl.DOCKER_IMAGE,
        "scope": "offline template and CRD validation; not live GKE/GitHub execution",
    }
    ctl.write_json(Path(out) / "chart-validation.json", receipt)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--out", default="build/validation")
    args = parser.parse_args()
    verify(ctl.load_config(args.config), args.out)
