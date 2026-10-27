# Standard self-hosted runner compatibility

The fleet uses GitHub's official Actions runner under ARC. Repository workflows
use the ordinary Actions feature set; the runner group and membership gate decide
who may submit work, not which repository secrets a permitted job may use.

| Feature | How it works |
|---|---|
| Repository / authorized organization secrets | Use `${{ secrets.NAME }}` through `env` or an action's `with` inputs; values are not automatically exported to every process |
| GitHub Environments | Set `environment: staging` on the job; its secrets, variables, reviewers and deployment branch rules apply normally |
| Repository / organization / environment variables | Use `${{ vars.NAME }}`; job/step variables use ordinary `env` |
| `GITHUB_TOKEN` | The job's `permissions:` control its repository access; the fleet does not substitute an operator token |
| OIDC | Set `id-token: write`; separately configure the target cloud's federation policy |
| Artifacts and Actions cache | Standard upload/download/cache actions use GitHub's services; local job storage remains disposable |
| Docker builds, job containers and service containers | Linux runners use a per-job DinD daemon on the 480 GiB job disk; images/actions must support the selected CPU architecture |
| Reusable workflows | Declare required secrets or use `secrets: inherit` from a trusted caller; environment secrets belong to jobs declaring that environment |
| `sudo` / package installation | Enabled inside the ephemeral runner container by default, using the supported image's passwordless sudo configuration |

## Secrets and environment example

Copy [examples/secrets-environment.yml](../examples/secrets-environment.yml) to an
authorized workflow path. Create repository secret `CI_API_TOKEN`, create a
`staging` GitHub Environment containing secret `DEPLOY_TOKEN` and variable
`API_URL`, and configure its approval/branch rules. The job explicitly references
each value without printing it. If the operator provides a separate protected target, release/publish/baseline-promotion jobs holding privileged credentials must select that protected group and its distinct labels (see [targets.md](targets.md)). Do not fall back to a general group when a protected job queues. The generic example must be adapted to the organization’s approved routing policy.

Organization secrets work when their repository
allowlist permits that repository. An environment attached to a different job
does not automatically apply to the GKE job.

For member-only PR execution, retain the
[trusted membership admission workflow](member-only-pr.md). Its separate Members:
read App key stays only in `gke-pr-admission`; the worker may use a different test
environment containing the appropriate job secrets. Do not pass the membership
App key or runner-management key to worker jobs. GitHub still withholds secrets
from untrusted fork/Dependabot PR contexts according to its normal rules.

## Package installation and filesystem

Profiles default to `allow_sudo: true`. The runner starts as UID 1001, while sudo
can elevate inside its own container. This enables ordinary commands such as
`sudo apt-get install` and Playwright's dependency installation. A custom image
must preserve the supported runner user's sudo configuration/group, or provide
its own compatible setup.

Set `allow_sudo: false` on a profile to disable privilege escalation and drop the
runner container's Linux capabilities. This is a command policy, not a hostile
multi-tenant isolation boundary, particularly while privileged DinD is enabled.

The runner writable layer reserves 2 GiB for scheduling but has no fixed 2 GiB
limit. Its actual capacity is bounded by the node's OS disk and kubelet eviction
thresholds. The separate 480 GiB PVC holds workspace, the setup-action tool cache,
temporary job files and Docker data; use those paths for large job outputs/caches.
Every runner is single-job and its files/package installations disappear after
cleanup. Preinstall common packages in an immutable custom image when useful for
startup time and reproducibility.

## Boundaries that remain

This is an ephemeral Linux container runner. Host systemd, node administration,
kernel modules, nested virtualization, Windows and macOS require a different
execution design. GitHub Environments and `sudo` inside the job do not grant GCP
node/operator credentials or Kubernetes cluster-admin rights. Private network
endpoints still require explicitly configured routes/egress rules. CPU/RAM,
workspace size, job lifetime and the selected architecture remain fleet resource
policies. These do not alter the normal GitHub secrets/token delivery protocol.

## Standard tool-cache path

All targets use `RUNNER_TOOL_CACHE=/opt/hostedtoolcache` and
`AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache`. That directory is a runner-owned
subdirectory of the same disposable job PVC as the workspace and Docker data;
it does not consume the node OS disk. DinD sees the same mount path so Docker
bind mounts can resolve it. Tools disappear with the job disk.

This follows the Ubuntu environment in
[actions/runner-images](https://github.com/actions/runner-images/blob/main/images/ubuntu/scripts/build/configure-environment.sh),
which sets both variables to `/opt/hostedtoolcache`. The
[setup-python self-hosted Ubuntu documentation](https://github.com/actions/setup-python/blob/v6/docs/advanced-usage.md#ubuntu)
also requires that the runner user can write the cache. That documentation
allows custom locations; this fleet deliberately uses the hosted-image location
to match prebuilt Python library search paths when child environments omit
`LD_LIBRARY_PATH`.

Run [the tool-cache regression workflow](../examples/toolcache.yml) on AMD64 and
ARM64. It checks Python 3.11/3.12 RUNPATH, the loaded libpython mapping, isolated
pip imports and clean-environment child processes. Template updates apply to new
runner pods; do not interrupt busy pods to replace their mounts.

The work-volume initializer clears setgid on directories after kubelet applies
`fsGroup`. Workspace, `_temp` and tool-cache roots remain runner-owned `1001:123`
with mode `0770`; newly created `mkdir(0700)` directories retain exactly `0700`
instead of inheriting `2700`. Pod `fsGroup: 123` and DinD's `--group=123` remain
in place for the separate Docker socket/externals volumes. The regression
workflow includes a GitHub-hosted directory-permission baseline and checks the
native pods' directory modes plus Docker socket access.
