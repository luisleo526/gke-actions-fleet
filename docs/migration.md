# Migrating existing workflows

Inventory local workflows before changing them. `scripts/audit_workflows.py` resolves common Git directories so worktrees are counted once, records local HEADs, and extracts runner selectors, matrix definitions, services, actions and compatibility signals. It does not fetch remote branches or execute any repository's code. Local inventory may differ from current remote workflows.

## Workload mapping

| Workload | Initial profile | Checks before moving |
|---|---|---|
| Notifications, path filters, metadata | `gke-2cpu-4gb-amd64` | Cold-start latency may exceed execution time; hosted runners can remain useful |
| Python unit tests, Node lint/type checks | `gke-4cpu-8gb-amd64` | Runtime version, native wheels, setup action tool cache |
| Browser E2E / frontend builds | `gke-8cpu-16gb-amd64` | Browser system libraries, browser cache, service networking, memory high-water mark |
| Native C++ / sanitizer / backend shards | `gke-16cpu-32gb-amd64` or `gke-32cpu-64gb-amd64` | Compiler/ABI, peak memory, explicit parallelism, disk size |
| ARM release builds / architecture tests | Matching `*-arm64` profile | All actions, binaries, wheels and service images must support ARM64 |
| macOS / Windows matrices | Existing OS runner | These Linux GKE pools cannot execute native macOS or Windows workloads |

These are starting points, not measured sizing guarantees. Collect elapsed time, CPU throttling, peak RSS, disk usage and queue latency on real jobs. A `32cpu` runner requires more than a 32-vCPU node because GKE and DinD also need capacity. The example uses N2 custom AMD64 machines and T2A ARM64 machines:

| Runner CPU / GiB | AMD64 node | ARM64 node |
|---|---|---|
| 2 / 4 | `n2-custom-4-12288` (4 CPU / 12 GiB) | `t2a-standard-4` (4 CPU / 16 GiB) |
| 4 / 8 | `n2-custom-6-16384` (6 CPU / 16 GiB) | `t2a-standard-8` (8 CPU / 32 GiB) |
| 8 / 16 | `n2-custom-10-24576` (10 CPU / 24 GiB) | `t2a-standard-16` (16 CPU / 64 GiB) |
| 16 / 32 | `n2-custom-18-45056` (18 CPU / 44 GiB) | `t2a-standard-32` (32 CPU / 128 GiB) |
| 32 / 64 | `n2-custom-36-77824` (36 CPU / 76 GiB) | `t2a-standard-48` (48 CPU / 192 GiB) |

T2A's fixed sizes leave significant unused capacity, especially memory. These defaults prioritize predictable fit and native ARM compatibility. Measure costs before adopting them at scale; smaller custom runner requests do not reduce the underlying VM price. New machine families may require different boot/work disk types and validator changes; C4A is not a drop-in replacement for the included PD Balanced layout.

## Toolchain image

The stock GitHub runner image is intentionally smaller than `ubuntu-latest`; it is not a hosted-runner image. [images/runner/Dockerfile](../images/runner/Dockerfile) supplies a baseline for CMake/Ninja/Clang/GCC, Eigen/MPFR, Python build dependencies, browser system libraries and a writable setup-action tool cache.

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  -f images/runner/Dockerfile -t ghcr.io/YOUR-ORG/gke-actions-fleet-runner:toolchain-1 --push .
docker buildx imagetools inspect ghcr.io/YOUR-ORG/gke-actions-fleet-runner:toolchain-1
```

Publish to a registry the nodes can pull from, such as a public GHCR package. Set the optional top-level `runner_image` to the multi-platform manifest digest, e.g. `ghcr.io/YOUR-ORG/gke-actions-fleet-runner@sha256:...`, then render/reinstall. A private registry requires separately configured image-pull access. Verify both platforms in the manifest. Base and DinD image digests supplied here have both platforms; no custom toolchain image is published by this repository.

Runner steps run as a non-root user with privilege escalation disabled. Move `sudo apt-get ...` into the image build. For Playwright, install OS libraries in the image and replace runtime `playwright install --with-deps chromium` with `playwright install chromium`; verify the browser version's full dependency list. Use `setup-node`/`setup-python` for explicit runtime versions. Add language/runtime-specific tools required by each repo; the baseline is not a complete hosted-runner replacement.

Workspace plus Docker images/build layers have **80 GiB total per job**. Large multi-platform builds must fit that budget; release flows that need more space must split jobs or export intermediate artifacts. Package caches and compiler caches must use explicit artifact/cache services; no local disk persists between jobs. Avoid storing confidential data in caches accessible to untrusted branches.

## Compatibility changes

- Replace legacy self-hosted label arrays and per-run dynamically generated labels with the exact ARC scale-set name. The pool name is the workflow routing contract. Existing webhook/VM controllers cannot service ARC names and must not compete for the same routing labels.
- Prefer `cmake --build ... -j "$RUNNER_CPU_LIMIT"` and `ctest --parallel "$RUNNER_CPU_LIMIT"`. `nproc` may report the larger node's CPU count, not the runner's cgroup quota. Sanitizer/test sharding can need separate memory limits and measured tuning.
- Pin architecture-specific release names and download URLs. A successful AMD64 test does not validate ARM wheels, native artifacts, `-march=native` behavior or QEMU builds. Run native architecture tests on both pools.
- DinD uses a private Docker daemon per runner. Service containers with published ports are reachable from shell steps at `localhost`; Docker job containers reach services through their Docker network. Validate workflows that assume host Docker socket paths or `--network=host`.
- Images using privileged DinD share a node trust boundary. Restrict repos/workflows by runner groups and avoid exposing this cluster to arbitrary fork code. Use separate infrastructure for hostile tenants.
- Private-cluster APIs and RFC1918 endpoints are blocked from runner pods by default. Explicitly design any required access; do not loosen all egress to make one deployment work. GitHub OIDC to public cloud APIs is the expected deployment authentication path.
- Pod lifetime is capped at 22,500 seconds, including scheduling/startup. Keep workflow timeouts at or below six hours and account for cold starts. Validate retry-safe jobs before enabling Spot; T2A profiles use on-demand VMs.

## Rollout and rollback

1. Keep the existing fleet online. Authorize only a canary repository in the new group and reduce max concurrency during first deployment.
2. Run all five sizes using `examples/smoke.yml`. Read job identities/results, then `wait-idle`; retain receipts in private operator storage.
3. Move representative Linux tests/builds behind a repo variable, for example `runs-on: ${{ vars.GKE_CI_ENABLED == 'true' && 'gke-8cpu-16gb-amd64' || 'ubuntu-latest' }}`. Keep privileged deployments and releases unchanged until their identities and artifacts are verified.
4. Compare behavior and artifacts for the exact source SHA, not only workflow success. Verify cancellation, failed jobs, queue saturation, Docker services and disk deletion.
5. Enable ARM jobs individually; preserve macOS/Windows legs. Expand runner-group access as repositories pass their gates.
6. Roll back by switching the repo variable/runner selector and routing new work to the old fleet. Let running GKE jobs finish. Retire old webhooks/controllers only after all intended workflows and queued jobs have migrated and their resources/registrations have been read back as gone.
