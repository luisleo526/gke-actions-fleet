# Operations

## Observe and accept

`runnerctl.py status` reads the configured Kubernetes context and GCP project, including ARC scale sets, runner resource metadata, PVC/PV objects, CSI disks selected by a cluster label, and each pool's managed instances. `wait-idle` repeats these reads and only succeeds when worker resources are absent. The system pool and controller intentionally remain running. Receipts contain operational metadata, not GitHub App or JIT secrets; keep receipts outside public commits.

At deployment record the source SHA, OpenTofu plan, provider lock, chart checksums, runner/DinD image digests and cluster identity. Run the smoke workflow for all five sizes; its job summaries record pod/node identity, actual architecture, cgroup CPU/memory limits and filesystem capacity. Save GitHub job conclusions separately. Also run a failed job, cancel a running job, cancel queued work, and exceed one profile's concurrency ceiling to check queue/recovery behavior.

For GitHub registration readback, use a separately authorized administrator or App installation token with the relevant runner-list permission. With an authorized `gh` session:

```sh
gh api --paginate /orgs/YOUR-ORG/actions/runners --jq '.runners[] | {id,name,status,busy}'
# Repository target instead:
gh api --paginate /repos/OWNER/REPO/actions/runners --jq '.runners[] | {id,name,status,busy}'
```

Match the pod/runner names recorded during the test; do not require unrelated fleets to disappear. The idle receipt covers ephemeral runner CRs, not an independent GitHub registration query. GKE/GitHub cloud tests have to run in the operator's environment; passing `make test` does not establish runtime behavior.

## Scaling and costs

Each profile creates a primary pool and optional alternative machine/zone pools in the same cluster. Alternative `max_nodes` budgets are subtracted from the primary pool; their sum remains the profile limit. AMD64 and ARM64 budgets are separate. A zonal control plane can serve the configured node-pool zones in its region. ARC's minRunners is zero; GKE total_min_node_count is zero. Required selectors prevent jobs landing on the wrong architecture or system pool. A cross-namespace required anti-affinity rule permits at most one concurrent runner per node. Busy pods declare `safe-to-evict: false`; after completion ARC removes the pod, enabling node scale-down. This does not protect against Spot eviction, node repair, upgrade or infrastructure failure. Keep workflow-level retries and capacity assumptions appropriate to the job.

VM startup, image pulls and disk provisioning add cold-start latency. Autoscaler scale-down and CSI garbage collection are asynchronous and cannot guarantee immediate billing cessation at job completion. The example uses AMD64 Spot primaries in zone a, matching Spot alt2 pools in zone b, and existing on-demand alt1 pools. ARM64 primaries and alt1 pools use on-demand for long-running jobs. Each profile's `spot` flag controls its primary pool; each alternative may independently set `spot`, defaulting to `false`. Array order does not establish autoscaler priority; verify actual selection in visibility logs. Idle runner VMs and work disks disappear, while GKE/system/NAT/logging costs remain. Autoscaling is not a billing budget. Configure quotas and GCP budget alerts independently, size NAT ports for outbound-heavy builds, and choose `max_runners` based on actual VM capacity/cost.

### Changing a primary pool's provisioning model

`spot` is immutable: changing it replaces the pool. Migrate only a few profiles at a time. Record timestamps, the original autoscaling maximum, pool/MIG identities, and the reviewed saved plan in private operator storage. Keep existing alternative pools and their budgets unchanged during a provisioning-model migration. Their capacity remains available while a primary is empty, without guaranteeing the full profile limit or autoscaler selection order.

1. Disable primary scale-up with `gcloud container node-pools update POOL --cluster CLUSTER --zone ZONE --project PROJECT --enable-autoscaling --total-min-nodes=0 --total-max-nodes=0`. These pools use total-node limits; use the matching flags. Do not resize, cordon, drain, evict, or delete busy nodes. Existing runner pods must finish naturally; idle primary nodes may still accept jobs until the autoscaler removes them.
2. Wait for zero Kubernetes nodes in that pool and verify every backing MIG has target size zero, no instances, and no pending instance actions. Check runner pods across all namespaces. If the pool still has nodes after 45 minutes, work on another profile and revisit it.
3. Change only that profile's `spot` value, render the deployment configuration, and run `tofu plan`. Apply the saved plan only when its changes are exactly the intended empty primary pool(s), restoring their original maximum. Recheck zero nodes/instances and no runner pods immediately before apply. Keep other profiles at their currently deployed setting during each batch.
4. Read back `config.spot`, machine type, and autoscaling maximum. Verify a real job on a node with `cloud.google.com/gke-spot=true`, review autoscaler selection logs, and check runner pod events for migration-caused termination. Finish with a full no-op plan.

To roll a profile back, repeat the same zero-node procedure with its previous `spot` value; its alternative pools and workflow selectors stay unchanged. If GCP rejects a T2A Spot pool in its existing zone, restore that profile to on-demand with a newly reviewed plan and report the exception without substituting a machine family. Keep Kubernetes credentials in a private `KUBECONFIG` and update changed administrator egress CIDRs through config/render/plan/apply.

The example maxima require 1,232 N2 Spot vCPUs, 864 N2D on-demand vCPUs, and 1,184 T2A on-demand vCPUs (480 primary + 704 alt1). The N2 Spot demand uses the separate PREEMPTIBLE_CPUS quota when available. At 176 runner nodes, 30 GB boot disks plus 480 GB work disks and the 50 GB system disk require 89,810 GB of SSD_TOTAL_GB capacity. Check regional limits and other usage before applying; a 500-vCPU T2A limit cannot satisfy these unchanged ARM budgets. Allow headroom for asynchronous disk deletion.

Each runner pod requests its configured job-disk size. `WaitForFirstConsumer` delays provisioning for unschedulable pods, including when the shared pool budget is exhausted. A bound disk can exist even if its pod never gets a job. Failed/canceled jobs must still result in pod deletion for Kubernetes GC to delete the claim and CSI disk. Data is not securely overwritten by this project; GCP disk deletion and provider storage guarantees apply. Job caches/artifacts uploaded elsewhere have their own retention policies.

## Disk variant rollout and measurement

Enable `workspace_variants_gib: [64, 128, 256, 512]` and explicitly retain each
target's `workspace_gib: 480` for phase 1. Ten profiles and four targets create
160 additional listeners, 200 total. Every variant has the base profile's
`maxRunners`, selector and required cross-namespace anti-affinity. All variants
and targets compete for the same bounded node pools; ARC's separate ceilings
can create additional Pending pods, but do not multiply active runner capacity.
At 176 nodes with 512 GiB job disks, 30 GB boot disks and four 50 GB system
disks, the configured worst-case SSD capacity is 95,592 GB, before other usage
and deletion lag. Check the actual regional quota and current usage.

Measure existing listeners with the metrics API and count system-node pods,
including DaemonSets. The original 110-pod limit cannot accommodate 200 listeners
on one node; two nodes also leave insufficient slots for system services.
Keep existing listener requests unchanged at 50 mCPU / 64 MiB. New disk
listeners request 10 mCPU / 32 MiB, with a 256 MiB memory limit and no CPU limit.
These requests should be checked against live usage and registration bursts.
A four-node `e2-standard-2` system pool provides 440 pod slots without changing
the per-node pod limit. Using us-central1 list-price estimates of
$0.06701142/hour per E2 standard-2 and $0.10/GB-month PD Balanced, its delta
over one E2 standard-4 with a 50 GB boot disk is about $112.84/month at 730 hours,
excluding taxes, discounts, logging and transient upgrade surge nodes. See
[Compute pricing](https://cloud.google.com/compute/vm-instance-pricing) and
[disk pricing](https://cloud.google.com/compute/disks-image-pricing).

Review a saved full OpenTofu plan at each step. Grow the system pool's node
count first, verify readiness and that no runner pods use it, then update its
machine type through GKE's rolling node update. Runner pools must have no plan
changes. Do not cordon/drain/evict runner nodes. Preserve the current local state.
Use the existing private `KUBECONFIG`; additions-only installation uses its
configured context and does not refresh credentials:

```sh
python scripts/runnerctl.py install --add-only --plan-only
# Review build/additions.json, values, and checksum-pinned chart templates.
python scripts/runnerctl.py install --add-only
```

Both commands read back every existing release's supplied Helm values before
any installation and record differences in `build/skipped-existing.json`.
Existing releases are always skipped, including pre-existing drift; a
non-deployed release fails the preflight. This mode never upgrades the
controller, existing scale sets, policies or storage class.
Verify all new listeners, PVC templates and unchanged base templates, run a real
job through an allowed workflow that supports the explicit suffix, and require
a final full no-op OpenTofu plan plus an empty additions plan.

Run `scripts/sample_disk_usage.py --context CONTEXT --output usage.jsonl
--exit-file sampler.exit` under a persistent session. It reads every runner
node's kubelet summary every 120 seconds for 24 hours, retaining only runner
work-PVC usage and operational identity. Heartbeat records preserve idle scans
and node/read errors. `python scripts/summarize_disk_usage.py usage.jsonl`
groups by target and scale set, counts distinct observed runner pod UIDs and
reports p50/p95/max of per-pod sampled peaks using nearest-rank percentiles.
Jobs shorter than the sampling interval can be missed, idle runner pods can
be counted, and peaks between samples are lower bounds; use this as sizing
evidence with headroom, not a complete job audit.

## Troubleshooting

| Symptom | Check |
|---|---|
| Jobs queue, no runner pod | App permissions/installation, group allowlist, exact `runs-on` name, ARC/listener logs |
| Pending runner pod | Correct architecture/taints, pool maximum, regional CPU quotas, machine availability, pod scheduling events |
| PVC Pending | PD CSI driver, `WaitForFirstConsumer` scheduling, disk quota, zone capacity |
| Job disk remains | Pod/PVC/PV finalizers and CSI controller events; inspect labeled GCP disks; do not force-remove finalizers before tracing ownership |
| VM stays after work | Pending/busy runner pods, system DaemonSets, autoscaler `noScaleDown` reasons; autoscaling is not instantaneous |
| Node MemoryPressure / image pull failure | Actual allocatable vs requests/DaemonSets, OS disk usage, registry permissions, architecture manifest |
| `sudo` fails | Check `allow_sudo`, the custom image's passwordless sudo configuration and sudo-group membership; the default profile permits sudo inside its runner container |
| Browser/Docker service fails | Image libraries, native ARM support, service port mapping, container vs shell networking |
| OIDC/deploy fails | Federated claims/audience/permissions; node credentials are intentionally unavailable |

Use `kubectl --context CONTEXT -n arc-system logs deployment/arc-controller` and target namespace events/logs. Forward logs before pods disappear; do not keep completed runner pods solely for logging because that delays disk cleanup. Avoid logging credentials from runner environment or Secret objects.

## Updates and removals

Runner versions have GitHub support deadlines. Review upstream releases regularly. Update the multi-platform runner index digest consistently in code and the optional Dockerfile, update DinD as needed, and run tests plus live canaries. `verify_charts.py` locks official chart archives by SHA-256. An ARC upgrade requires **both chart checksums/version and compatible CRDs**; Helm does not upgrade CRDs automatically. Read the release's upgrade notes, back up non-secret resource metadata, inspect the new CRDs from the checksum-verified controller chart, apply the reviewed CRDs, then upgrade controller and scale sets.

`install` does not prune removed targets/profiles. To retire a profile, stop routing new jobs to its name, let its work drain, and uninstall its scale-set Helm release in each target namespace while the ARC controller is still running. Verify finalizer-driven GitHub cleanup, PVC/PV/disk deletion and zero instances before removing its Terraform profile. Otherwise IaC deletion could terminate active jobs. Remove target namespaces/Secrets only after ARC cleanup; never delete the controller before scale sets finish finalizing.

To destroy the whole deployment, drain and uninstall all scale-set releases first, verify cloud/GitHub cleanup, uninstall the controller, then set Terraform `deletion_protection=false` in a reviewed apply and use a reviewed destroy plan with the same variable file/state. Default deletion protection guards accidental cluster deletion. The state also binds its project with `terraform_data.deployment_identity` and `prevent_destroy`; deliberate decommissioning must explicitly review/remove that guard as well. Never change `project_id` to migrate an existing state. No teardown command is executed automatically by these tools.

References: [GKE cluster autoscaler](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/cluster-autoscaler), [GKE node resource reservations](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/plan-node-sizes), [generic ephemeral volumes](https://kubernetes.io/docs/concepts/storage/ephemeral-volumes/#generic-ephemeral-volumes), [ARC release 0.15.1](https://github.com/actions/actions-runner-controller/releases/tag/gha-runner-scale-set-0.15.1).

Validation outputs use `build/validation/`; deployment parameters use `build/terraform.tfvars.json`. Do not share a render output directory between example/CI validation and a live deployment. Check the saved plan project, all resource actions and expected pool budgets before any apply; use fail-fast orchestration and never apply after a failed review.
