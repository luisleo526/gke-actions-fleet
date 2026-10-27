#!/usr/bin/env python3
"""Render and install official ARC scale sets; no Python dependencies or cloud SDK imports."""

import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARC_VERSION = "0.15.1"
CHARTS = "oci://ghcr.io/actions/actions-runner-controller-charts/"
RUNNER_IMAGE = (
    "ghcr.io/actions/actions-runner:2.338.2@sha256:759a1d94a15b05941aba5196974ff43fe15b4cbba29b694b35cbc32a1166b232"
)
DOCKER_IMAGE = "docker:29.8.1-dind@sha256:3f3c01aaaebf7cce837356b688b7c059a4749f10bd7660dec7c58fc454a283f0"
CHART_HASHES = {
    "gha-runner-scale-set": "233b3c53a7c5ebe91f24ca87736f4b76caf1ae2079482a1d036da828cbe247b1",
    "gha-runner-scale-set-controller": "248807b8ff55fd1a5cc7bb5250558092a162637cfb875ba041eddeeaa51cd747",
}
PREFIX = "gke-gh-runner.io/"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def keys(value, required, optional=()):
    require(isinstance(value, dict), "Expected an object")
    require(set(required) <= value.keys(), f"Missing keys: {set(required) - value.keys()}")
    require(
        value.keys() <= set(required) | set(optional), f"Unknown keys: {value.keys() - set(required) - set(optional)}"
    )


def name(value, maximum=40):
    require(
        isinstance(value, str) and len(value) <= maximum and re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value),
        f"Invalid name: {value!r}",
    )


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, f"{label} must be an integer in {low}..{high}")


def validate_machine(profile_name, p, machine_type):
    match = re.fullmatch(r"(e2|n2|n2d|t2a)-standard-(\d+)", machine_type)
    custom = re.fullmatch(r"n2-custom-(\d+)-(\d+)", machine_type)
    require(
        match is not None or custom is not None, "Supported machines: e2/n2/n2d/t2a-standard-N or n2-custom-CPU-MiB"
    )
    family, cpus = (match[1], int(match[2])) if match else ("n2", int(custom[1]))
    memory = cpus * 4 if match else int(custom[2]) / 1024
    if custom:
        require(
            (2 <= cpus <= 32 and cpus % 2 == 0) or (36 <= cpus <= 80 and cpus % 4 == 0),
            "Invalid N2 custom vCPU count",
        )
        require(int(custom[2]) % 256 == 0 and cpus * 0.5 <= memory <= cpus * 8, "Invalid N2 custom memory")
    if family == "t2a":
        require(cpus in {1, 2, 4, 8, 16, 32, 48}, "Invalid T2A vCPU count")
    expected = "arm64" if family == "t2a" else "amd64"
    require(p["architecture"] == expected, f"{profile_name}: machine requires {expected}")
    integer(p["cpu"], 1, cpus - 1, "cpu (reserve at least one vCPU for GKE and Docker)")
    # GKE's reservation tiers, eviction margin, DaemonSets and baseline Docker overhead.
    remaining, reserved = memory, 0.1
    for width, rate in [(4, 0.25), (4, 0.20), (8, 0.10), (112, 0.06), (float("inf"), 0.02)]:
        portion = min(remaining, width)
        reserved += portion * rate
        remaining -= portion
    integer(
        p["memory_gib"], 1, int(memory - reserved - 3), "memory_gib (must leave GKE, DaemonSet and Docker headroom)"
    )
    require(type(p["spot"]) is bool and type(p["docker"]) is bool, "spot/docker must be booleans")
    require(not (family == "t2a" and p["spot"]), "T2A profiles use on-demand; choose another supported family for Spot")


def load_config(path):
    c = json.loads(Path(path).read_text())
    keys(c, ["project_id", "region", "zone", "cluster_name", "admin_cidrs", "targets", "profiles"], ["runner_image"])
    if "runner_image" in c:
        require(
            isinstance(c["runner_image"], str)
            and re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", c["runner_image"]),
            "runner_image must use an immutable image digest",
        )
    require(
        isinstance(c["project_id"], str) and re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", c["project_id"]),
        "Invalid GCP project ID",
    )
    name(c["cluster_name"], 24)  # leaves room for the node service account suffix
    require(isinstance(c["region"], str) and re.fullmatch(r"[a-z]+-[a-z]+\d+", c["region"]), "Invalid region")
    require(
        isinstance(c["zone"], str) and re.fullmatch(re.escape(c["region"]) + r"-[a-z]", c["zone"]),
        "zone must belong to region",
    )
    require(isinstance(c["admin_cidrs"], list) and c["admin_cidrs"], "admin_cidrs must not be empty")
    for cidr in c["admin_cidrs"]:
        network = ipaddress.ip_network(cidr)
        require(
            network.version == 4 and network.prefixlen >= 8, "Use explicit IPv4 administrator CIDRs of /8 or narrower"
        )
    require(isinstance(c["profiles"], dict) and c["profiles"], "profiles must not be empty")
    for profile_name, p in c["profiles"].items():
        name(profile_name)
        keys(
            p,
            ["architecture", "machine_type", "cpu", "memory_gib", "max_runners", "spot", "docker"],
            ["alternatives", "allow_sudo"],
        )
        require(type(p.get("allow_sudo", True)) is bool, "allow_sudo must be a boolean")
        validate_machine(profile_name, p, p["machine_type"])
        integer(p["max_runners"], 1, 100, "max_runners")
        alternatives = p.get("alternatives", [])
        require(isinstance(alternatives, list), "alternatives must be a list")
        allocated = 0
        seen = {(p["machine_type"], c["zone"])}
        for alternative in alternatives:
            keys(alternative, ["machine_type", "max_nodes"], ["zone"])
            integer(alternative["max_nodes"], 1, p["max_runners"], "alternative.max_nodes")
            zone = alternative.get("zone", c["zone"])
            require(
                isinstance(zone, str) and re.fullmatch(re.escape(c["region"]) + r"-[a-z]", zone),
                "Alternative zone must be in the cluster region",
            )
            identity = (alternative["machine_type"], zone)
            require(identity not in seen, "Duplicate machine/zone candidate")
            seen.add(identity)
            validate_machine(profile_name, p, alternative["machine_type"])
            allocated += alternative["max_nodes"]
        require(allocated < p["max_runners"], "Alternative budgets must leave at least one primary node slot")
    require(isinstance(c["targets"], dict) and c["targets"], "targets must not be empty")
    urls = set()
    for target_name, target in c["targets"].items():
        name(target_name)
        keys(target, ["github_url", "github_secret"], ["runner_group"])
        name(target["github_secret"])
        require(
            isinstance(target["github_url"], str)
            and re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", target["github_url"]),
            "Use a github.com organization or repository URL without a trailing slash",
        )
        normalized = target["github_url"].lower()
        require(normalized not in urls, "Duplicate GitHub target: scale set names must be unique within a target/group")
        urls.add(normalized)
        if "runner_group" in target:
            require(
                isinstance(target["runner_group"], str) and target["runner_group"].strip(),
                "runner_group must not be empty",
            )
            require(target["github_url"].count("/") == 3, "runner_group is only valid for an organization target")
    return c


def namespace(target):
    return f"arc-{target}"


def node_pool_variants(c):
    """Split a profile's global capacity across compatible machine/zone pools."""
    pools = {}
    for profile_name, profile in c["profiles"].items():
        alternatives = profile.get("alternatives", [])
        primary = {k: profile[k] for k in ("architecture", "machine_type", "spot", "max_runners")}
        primary.update(profile_name=profile_name, zone=c["zone"])
        primary["max_runners"] -= sum(a["max_nodes"] for a in alternatives)
        require(profile_name not in pools, "Node pool name collision")
        pools[profile_name] = primary
        for index, alternative in enumerate(alternatives, start=1):
            pool_name = f"{profile_name}-alt{index}"
            if len(pool_name) > 40:
                pool_name = profile_name[:31] + "-" + hashlib.sha256(pool_name.encode()).hexdigest()[:8]
            require(pool_name not in pools and pool_name not in c["profiles"], "Node pool name collision")
            pools[pool_name] = {
                **primary,
                "machine_type": alternative["machine_type"],
                "max_runners": alternative["max_nodes"],
                "zone": alternative.get("zone", c["zone"]),
            }
    return pools


def context(c):
    return f"gke_{c['project_id']}_{c['zone']}_{c['cluster_name']}"


def resources(cpu, memory, storage):
    return {
        "requests": {"cpu": str(cpu), "memory": memory, "ephemeral-storage": storage},
        "limits": {"cpu": str(cpu), "memory": memory, "ephemeral-storage": storage},
    }


def scale_values(c, target_name, profile_name):
    p, target = c["profiles"][profile_name], c["targets"][target_name]
    runner_image = c.get("runner_image", RUNNER_IMAGE)
    allow_sudo = p.get("allow_sudo", True)
    runner = {
        "name": "runner",
        "image": runner_image,
        "command": ["/home/runner/run.sh"],
        "resources": resources(p["cpu"], f"{p['memory_gib']}Gi", "2Gi"),
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1001,
            "runAsGroup": 123,
            "allowPrivilegeEscalation": allow_sudo,
        },
        "env": [
            {"name": "RUNNER_PROFILE", "value": profile_name},
            {"name": "RUNNER_ARCHITECTURE", "value": p["architecture"]},
            {"name": "RUNNER_TOOL_CACHE", "value": "/home/runner/_work/_tool"},
            {"name": "AGENT_TOOLSDIRECTORY", "value": "/home/runner/_work/_tool"},
            {"name": "TMPDIR", "value": "/home/runner/_work/_temp"},
            {"name": "RUNNER_POD", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            {"name": "RUNNER_NODE", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
            {
                "name": "RUNNER_CPU_LIMIT",
                "valueFrom": {
                    "resourceFieldRef": {"containerName": "runner", "resource": "limits.cpu", "divisor": "1"}
                },
            },
            {
                "name": "RUNNER_MEMORY_LIMIT",
                "valueFrom": {
                    "resourceFieldRef": {"containerName": "runner", "resource": "limits.memory", "divisor": "1"}
                },
            },
        ],
        "volumeMounts": [{"name": "work", "mountPath": "/home/runner/_work", "subPath": "workspace"}],
    }
    # Reserve writable-layer space without imposing a 2 GiB package-install cap.
    # The node OS disk still bounds this storage; workspace/Docker use their 80 GiB PVC.
    del runner["resources"]["limits"]["ephemeral-storage"]
    if not allow_sudo:
        runner["securityContext"]["capabilities"] = {"drop": ["ALL"]}
    spec = {
        "nodeSelector": {PREFIX + "profile": profile_name, "kubernetes.io/arch": p["architecture"]},
        "tolerations": [
            {"key": PREFIX + "profile", "operator": "Equal", "value": profile_name, "effect": "NoSchedule"}
        ],
        "automountServiceAccountToken": False,
        "securityContext": {"fsGroup": 123},
        "restartPolicy": "Never",
        "activeDeadlineSeconds": 22500,
        "terminationGracePeriodSeconds": 60,
        "affinity": {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {"matchLabels": {PREFIX + "workload": "runner"}},
                        "namespaceSelector": {},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        },
        "containers": [runner],
        "initContainers": [
            {
                "name": "init-work",
                "image": runner_image,
                "command": [
                    "/bin/bash",
                    "-ec",
                    "mkdir -p /mnt/work/workspace/_tool /mnt/work/workspace/_temp /mnt/work/docker; chown -R 1001:123 /mnt/work/workspace; chmod 0770 /mnt/work/workspace",
                ],
                "securityContext": {"runAsUser": 0},
                "resources": resources("100m", "128Mi", "1Gi"),
                "volumeMounts": [{"name": "work", "mountPath": "/mnt/work"}],
            }
        ],
        "volumes": [
            {
                "name": "work",
                "ephemeral": {
                    "volumeClaimTemplate": {
                        "metadata": {"labels": {PREFIX + "workload": "runner"}},
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "storageClassName": "runner-work",
                            "resources": {"requests": {"storage": "80Gi"}},
                        },
                    }
                },
            }
        ],
    }
    if allow_sudo:
        # The supported runner base image grants passwordless sudo to GID 27.
        spec["securityContext"]["supplementalGroups"] = [27]
    if p["docker"]:
        runner["env"] += [
            {"name": "DOCKER_HOST", "value": "unix:///var/run/docker.sock"},
            {"name": "RUNNER_WAIT_FOR_DOCKER_IN_SECONDS", "value": "120"},
        ]
        runner["volumeMounts"].append({"name": "dind-sock", "mountPath": "/var/run"})
        spec["initContainers"] += [
            {
                "name": "init-dind-externals",
                "image": runner_image,
                "command": ["cp", "-r", "/home/runner/externals/.", "/home/runner/tmpDir/"],
                "resources": resources("100m", "128Mi", "1Gi"),
                "volumeMounts": [{"name": "dind-externals", "mountPath": "/home/runner/tmpDir"}],
            },
            {
                "name": "dind",
                "image": DOCKER_IMAGE,
                "restartPolicy": "Always",
                "args": ["dockerd", "--host=unix:///var/run/docker.sock", "--group=123"],
                "securityContext": {"privileged": True, "runAsUser": 0},
                "resources": {
                    "requests": {"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "2Gi"},
                    "limits": {"cpu": str(p["cpu"]), "memory": f"{p['memory_gib']}Gi", "ephemeral-storage": "2Gi"},
                },
                "startupProbe": {"exec": {"command": ["docker", "info"]}, "periodSeconds": 5, "failureThreshold": 24},
                "volumeMounts": [
                    {"name": "work", "mountPath": "/home/runner/_work", "subPath": "workspace"},
                    {"name": "dind-sock", "mountPath": "/var/run"},
                    {"name": "dind-externals", "mountPath": "/home/runner/externals"},
                    {"name": "work", "mountPath": "/var/lib/docker", "subPath": "docker"},
                ],
            },
        ]
        spec["volumes"] += [
            {"name": "dind-sock", "emptyDir": {}},
            {"name": "dind-externals", "emptyDir": {"sizeLimit": "1Gi"}},
        ]
    values = {
        "githubConfigUrl": target["github_url"],
        "githubConfigSecret": target["github_secret"],
        "runnerScaleSetName": profile_name,
        "minRunners": 0,
        "maxRunners": p["max_runners"],
        "controllerServiceAccount": {"namespace": "arc-system", "name": "arc-controller"},
        "listenerTemplate": {
            "spec": {
                "nodeSelector": {PREFIX + "pool": "system"},
                "containers": [
                    {
                        "name": "listener",
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                    }
                ],
            }
        },
        "template": {
            "metadata": {
                "labels": {PREFIX + "workload": "runner"},
                "annotations": {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"},
            },
            "spec": spec,
        },
    }
    if target.get("runner_group"):
        values["runnerGroup"] = target["runner_group"]
    return values


def namespace_manifest(target):
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace(target),
            "labels": {PREFIX + "managed": "true", "pod-security.kubernetes.io/enforce": "privileged"},
        },
    }


def network_policy(target):
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "runner-isolation", "namespace": namespace(target)},
        "spec": {
            "podSelector": {"matchLabels": {PREFIX + "workload": "runner"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                            "podSelector": {
                                "matchExpressions": [
                                    {"key": "k8s-app", "operator": "In", "values": ["kube-dns", "node-local-dns"]}
                                ]
                            },
                        },
                        {"ipBlock": {"cidr": "169.254.20.10/32"}},
                        # GKE Dataplane V2 can evaluate DNS egress before service DNAT.
                        {"ipBlock": {"cidr": "10.45.0.10/32"}},
                    ],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                },
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": [
                                    "10.0.0.0/8",
                                    "172.16.0.0/12",
                                    "192.168.0.0/16",
                                    "169.254.0.0/16",
                                    "100.64.0.0/10",
                                    "127.0.0.0/8",
                                ],
                            }
                        }
                    ]
                },
            ],
        },
    }


def storage_class(c):
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": "runner-work"},
        "provisioner": "pd.csi.storage.gke.io",
        "volumeBindingMode": "WaitForFirstConsumer",
        "reclaimPolicy": "Delete",
        "parameters": {
            "type": "pd-balanced",
            "csi.storage.k8s.io/fstype": "ext4",
            "labels": f"gke-gh-runner-cluster={c['cluster_name']}",
        },
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def render(c, out):
    out = Path(out)
    tf = {k: c[k] for k in ("project_id", "region", "zone", "cluster_name", "admin_cidrs")}
    tf["profiles"] = node_pool_variants(c)
    write_json(out / "terraform.tfvars.json", tf)
    write_json(out / "storage-class.json", storage_class(c))
    write_json(
        out / "controller.json",
        {
            "fullnameOverride": "arc-controller",
            "serviceAccount": {"name": "arc-controller"},
            "nodeSelector": {PREFIX + "pool": "system"},
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
            "flags": {"logLevel": "info", "logFormat": "json"},
        },
    )
    for target in c["targets"]:
        write_json(out / target / "namespace.json", namespace_manifest(target))
        write_json(out / target / "network-policy.json", network_policy(target))
        for profile in c["profiles"]:
            write_json(out / target / f"{profile}.json", scale_values(c, target, profile))


def run(args, data=None, capture=False):
    return subprocess.run(args, input=data, text=True, check=True, capture_output=capture, timeout=1200).stdout


def kube(c, args, data=None, capture=False):
    return run(["kubectl", "--context", context(c), *args], data, capture)


def apply_json(c, value):
    # Server-side apply prevents kubectl's last-applied annotation from copying credentials.
    kube(c, ["apply", "--server-side", "--field-manager=gke-gh-runner", "-f", "-"], json.dumps(value))


def fetch_charts(out):
    directory = Path(out) / "charts"
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for chart, expected in CHART_HASHES.items():
        archive = directory / f"{chart}-{ARC_VERSION}.tgz"
        if not archive.exists():
            run(["helm", "pull", CHARTS + chart, "--version", ARC_VERSION, "--destination", str(directory)])
        require(hashlib.sha256(archive.read_bytes()).hexdigest() == expected, f"Chart checksum mismatch: {archive}")
        paths[chart] = str(archive)
    return paths


def install(c, out):
    render(c, out)
    charts = fetch_charts(out)
    run(
        [
            "gcloud",
            "container",
            "clusters",
            "get-credentials",
            c["cluster_name"],
            "--zone",
            c["zone"],
            "--project",
            c["project_id"],
        ]
    )
    for target in c["targets"]:
        apply_json(c, namespace_manifest(target))
        # Check every credential before changing any Helm releases.
        kube(c, ["get", "secret", c["targets"][target]["github_secret"], "-n", namespace(target)], capture=True)
    apply_json(c, storage_class(c))
    common = ["--kube-context", context(c), "--version", ARC_VERSION, "--wait", "--timeout", "10m"]
    run(
        [
            "helm",
            "upgrade",
            "--install",
            "arc-controller",
            charts["gha-runner-scale-set-controller"],
            "--namespace",
            "arc-system",
            "--create-namespace",
            "-f",
            str(Path(out) / "controller.json"),
            *common,
        ]
    )
    for target in c["targets"]:
        apply_json(c, network_policy(target))
        for profile in c["profiles"]:
            run(
                [
                    "helm",
                    "upgrade",
                    "--install",
                    profile,
                    charts["gha-runner-scale-set"],
                    "--namespace",
                    namespace(target),
                    "-f",
                    str(Path(out) / target / f"{profile}.json"),
                    *common,
                ]
            )


def snapshot(c):
    result = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "context": context(c),
        "targets": {},
    }
    result["nodes"] = json.loads(kube(c, ["get", "nodes", "-l", PREFIX + "pool=runner", "-o", "json"], capture=True))[
        "items"
    ]
    for target in c["targets"]:
        ns = namespace(target)
        sets = json.loads(kube(c, ["get", "autoscalingrunnersets", "-n", ns, "-o", "json"], capture=True))["items"]
        require(
            {s["metadata"]["name"] for s in sets} == set(c["profiles"]),
            f"{ns}: installed scale sets differ from configuration",
        )
        result["targets"][target] = {"scale_sets": sets}
        for key, resource in [("pods", "pods"), ("runners", "ephemeralrunners"), ("claims", "pvc")]:
            args = ["get", resource, "-n", ns, "-o", "json"]
            if key == "pods":
                args += ["-l", PREFIX + "workload=runner"]
            items = json.loads(kube(c, args, capture=True))["items"]
            # Runner CR specs may contain JIT registration material. Receipts retain metadata only.
            result["targets"][target][key] = [
                {
                    "name": item["metadata"]["name"],
                    "uid": item["metadata"].get("uid"),
                    "deletionTimestamp": item["metadata"].get("deletionTimestamp"),
                    "phase": item.get("status", {}).get("phase"),
                }
                for item in items
            ]
    volumes = json.loads(kube(c, ["get", "pv", "-o", "json"], capture=True))["items"]
    result["volumes"] = [v for v in volumes if v.get("spec", {}).get("storageClassName") == "runner-work"]
    result["disks"] = json.loads(
        run(
            [
                "gcloud",
                "compute",
                "disks",
                "list",
                "--project",
                c["project_id"],
                "--filter",
                f"labels.gke-gh-runner-cluster={c['cluster_name']}",
                "--format=json",
            ],
            capture=True,
        )
    )
    # Verify cloud MIG sizes too: Node objects alone do not prove VM deletion.
    result["instance_groups"] = {}
    for profile in node_pool_variants(c):
        pool = json.loads(
            run(
                [
                    "gcloud",
                    "container",
                    "node-pools",
                    "describe",
                    profile,
                    "--cluster",
                    c["cluster_name"],
                    "--zone",
                    c["zone"],
                    "--project",
                    c["project_id"],
                    "--format=json",
                ],
                capture=True,
            )
        )
        groups = pool.get("instanceGroupUrls", [])
        require(bool(groups), f"{profile}: no instance groups returned")
        for url in groups:
            project, zone, group = parse_group_url(url)
            require(project == c["project_id"], f"Unexpected project in instance group URL: {url}")
            group_info = json.loads(
                run(
                    [
                        "gcloud",
                        "compute",
                        "instance-groups",
                        "managed",
                        "describe",
                        group,
                        "--project",
                        project,
                        "--zone",
                        zone,
                        "--format=json",
                    ],
                    capture=True,
                )
            )
            instances = json.loads(
                run(
                    [
                        "gcloud",
                        "compute",
                        "instance-groups",
                        "managed",
                        "list-instances",
                        group,
                        "--project",
                        project,
                        "--zone",
                        zone,
                        "--format=json",
                    ],
                    capture=True,
                )
            )
            require("targetSize" in group_info, f"{group}: missing target size")
            result["instance_groups"][url] = {
                "target_size": group_info["targetSize"],
                "instances": instances,
                "current_actions": group_info.get("currentActions", {}),
            }
    return result


def parse_group_url(url):
    # GKE returns instanceGroups links; some APIs/receipts use manager links.
    match = re.fullmatch(
        r"https://www.googleapis.com/compute/v1/projects/([^/]+)/zones/([^/]+)/instanceGroup(?:Managers|s)/([^/]+)", url
    )
    require(match is not None, f"Unrecognized zonal instance group URL: {url}")
    return match.groups()


def is_idle(state):
    return (
        not state["nodes"]
        and not state["volumes"]
        and not state["disks"]
        and all(not t["pods"] and not t["runners"] and not t["claims"] for t in state["targets"].values())
        and bool(state["instance_groups"])
        and all(
            group["target_size"] == 0
            and not group["instances"]
            and all(count == 0 for action, count in group["current_actions"].items() if action != "none")
            for group in state["instance_groups"].values()
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    for command in ("render", "install"):
        sub.add_parser(command).add_argument("--out", default="build")
    secret = sub.add_parser("create-secret")
    secret.add_argument("--target", required=True)
    secret.add_argument("--app-id", required=True)
    secret.add_argument("--installation-id", required=True)
    secret.add_argument("--private-key", type=Path, required=True)
    sub.add_parser("status")
    idle = sub.add_parser("wait-idle")
    idle.add_argument("--timeout", type=int, default=1800)
    idle.add_argument("--receipt", type=Path, default=Path("build/idle-receipt.json"))
    args = parser.parse_args()
    c = load_config(args.config)
    if args.command == "validate":
        print(f"Valid: one cluster, {len(c['profiles'])} profiles, {len(c['targets'])} GitHub targets")
    elif args.command == "render":
        render(c, args.out)
        print(f"Rendered to {args.out}")
    elif args.command == "install":
        install(c, args.out)
    elif args.command == "create-secret":
        require(args.target in c["targets"], "Unknown target")
        require(args.app_id.isdigit() and args.installation_id.isdigit(), "App and installation IDs must be numeric")
        key = args.private_key.read_text()
        require("PRIVATE KEY-----" in key, "Expected a PEM private key")
        apply_json(c, namespace_manifest(args.target))
        credentials = {
            "github_app_id": args.app_id,
            "github_app_installation_id": args.installation_id,
            "github_app_private_key": key,
        }
        apply_json(
            c,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {"name": c["targets"][args.target]["github_secret"], "namespace": namespace(args.target)},
                "data": {k: base64.b64encode(v.encode()).decode() for k, v in credentials.items()},
            },
        )
    elif args.command == "status":
        print(json.dumps(snapshot(c), indent=2))
    elif args.command == "wait-idle":
        require(args.timeout > 0, "timeout must be positive")
        deadline = time.monotonic() + args.timeout
        while True:
            state = snapshot(c)
            state["idle"] = is_idle(state)
            write_json(args.receipt, state)
            if state["idle"]:
                print(f"Zero runner pods, ephemeral runners, work disks and managed instances: {args.receipt}")
                break
            require(time.monotonic() < deadline, f"Scale-down timed out; inspect {args.receipt}")
            print("Waiting for runner pools to reach zero...", flush=True)
            time.sleep(min(20, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Never include subprocess stdin: it may contain the GitHub App private key.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
