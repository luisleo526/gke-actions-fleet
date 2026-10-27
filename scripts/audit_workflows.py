#!/usr/bin/env python3
"""Read-only workflow inventory. Reports stay in ignored build/ by default."""

import argparse
import json
import re
import subprocess
from pathlib import Path

import yaml


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()


def audit(root, patterns):
    repositories = {}
    for pattern in patterns:
        for candidate in sorted(Path(root).expanduser().glob(pattern)):
            if not candidate.is_dir():
                continue
            try:
                common = Path(git(candidate, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            except subprocess.CalledProcessError:
                continue
            # Worktrees share a common .git directory; inventory the canonical checkout once.
            repo = common.parent if common.name == ".git" else candidate
            if str(common) in repositories:
                continue
            workflows = []
            for path in sorted((repo / ".github/workflows").glob("*.y*ml")):
                document = yaml.safe_load(path.read_text()) or {}
                jobs = []
                for job_name, job in document.get("jobs", {}).items():
                    steps = job.get("steps", [])
                    commands = "\n".join(str(step.get("run", "")) for step in steps)
                    uses = sorted({str(step["uses"]) for step in steps if "uses" in step})
                    jobs.append(
                        {
                            "job": job_name,
                            "runs_on": job.get("runs-on"),
                            "reusable_workflow": job.get("uses"),
                            "matrix": job.get("strategy", {}).get("matrix"),
                            "services": list(job.get("services", {})),
                            "container": job.get("container"),
                            "timeout_minutes": job.get("timeout-minutes"),
                            "actions": uses,
                            "features": [
                                name
                                for name, regex in {
                                    "docker": r"\bdocker\b|build-push-action|setup-buildx-action",
                                    "sudo_or_apt": r"\bsudo\b|\bapt(?:-get)?\b",
                                    "native_compilation": r"\b(?:cmake|ctest|ninja|gcc|g\+\+|clang|maturin|cibuildwheel)\b",
                                    "browser": r"playwright|puppeteer|cypress",
                                    "cloud_auth": r"google-github-actions/auth|aws-actions/configure-aws-credentials|wrangler",
                                    "host_tool_cache": r"RUNNER_TOOL_CACHE|/opt/hostedtoolcache|/opt/pipx|AGENT_TOOLSDIRECTORY",
                                    "host_network": r"--network[= ]host|\blocalhost\b|127\.0\.0\.1",
                                    "fixed_arch_binary": r"x86_64|linux-amd64|linux_x64|aarch64|linux-arm64",
                                }.items()
                                if re.search(regex, commands + "\n" + "\n".join(uses), re.I)
                            ],
                        }
                    )
                workflows.append({"file": path.name, "jobs": jobs})
            if not workflows:
                continue
            repositories[str(common)] = {
                "repository": repo.name,
                "path": str(repo),
                "local_head": git(repo, "rev-parse", "HEAD"),
                "workflows": workflows,
            }
    return {
        "source": "local canonical checkouts; not remote HEAD; no repositories modified",
        "repositories": list(repositories.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="~/code")
    parser.add_argument("--pattern", action="append", required=True)
    parser.add_argument("--out", type=Path, default=Path("build/migration-inventory.json"))
    args = parser.parse_args()
    report = audit(args.root, args.pattern)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    count = sum(len(w["jobs"]) for r in report["repositories"] for w in r["workflows"])
    print(f"Inventoried {len(report['repositories'])} repositories, {count} jobs: {args.out}")


if __name__ == "__main__":
    main()
