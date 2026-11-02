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

One 480 GiB disk is created per runner pod, even if that pod never gets a job. Failed/canceled jobs must still result in pod deletion for Kubernetes GC to delete the claim and CSI disk. Data is not securely overwritten by this project; GCP disk deletion and provider storage guarantees apply. Job caches/artifacts uploaded elsewhere have their own retention policies.

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
