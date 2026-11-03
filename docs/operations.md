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

VM startup, image pulls and disk provisioning add cold-start latency. Autoscaler scale-down and CSI garbage collection are asynchronous and cannot guarantee immediate billing cessation at job completion. The example uses four AMD64 Spot pools per profile: N2 custom primary/alt2 in zones a/b, N2D custom alt3 in c, and E2 custom alt4 in f (N2D custom for the 32cpu profile, which exceeds E2's CPU ceiling with overhead). Each Spot pool receives one quarter of the Spot budget; on-demand alt1 retains eight slots. ARM64 primaries and alt1 pools use on-demand for long-running jobs. Each profile's `spot` flag controls its primary pool; each alternative may independently set `spot`, defaulting to `false`. Array order does not establish autoscaler priority. GKE considers Spot discounts when expanding pools and normally prefers Spot to on-demand, subject to capacity, backoff and scheduling constraints. It does not guarantee the cheapest pool among several Spot pools. Verify `scaleUp.increasedMigs` and triggering pods in autoscaler visibility logs; existing free nodes can receive jobs without a scale-up. No custom ComputeClass or workload migration is needed for this pool-based diversification. Idle runner VMs and work disks disappear, while GKE/system/NAT/logging costs remain. Autoscaling is not a billing budget. Configure quotas and GCP budget alerts independently, size NAT ports for outbound-heavy builds, and choose `max_runners` based on actual VM capacity/cost.

### Adding Spot families and zones

Append alternatives to preserve existing alt numbers. Check machine availability in every selected zone using `gcloud compute machine-types describe`, and verify both boot and job disk support in the [machine-series documentation](https://docs.cloud.google.com/compute/docs/general-purpose-machines). N2, N2D and E2 support PD Balanced; avoid Hyperdisk-only families. Lower existing autoscaling maxima and add the new zero-node pools using a reviewed full plan containing only those pool changes. Changing an autoscaling ceiling does not resize or evict busy nodes; `safe-to-evict: false` prevents normal autoscaler removal while runners work. Existing nodes above a new maximum finish naturally, so a temporary live-node overage is possible. Never use a node-pool resize to enforce the new ceiling. Machine/Spot changes to an existing pool require the empty-pool procedure below. Do not reinstall ARC for pool-only changes.

### Changing a primary pool's provisioning model

`spot` is immutable: changing it replaces the pool. Migrate only a few profiles at a time. Record timestamps, the original autoscaling maximum, pool/MIG identities, and the reviewed saved plan in private operator storage. Keep existing alternative pools and their budgets unchanged during a provisioning-model migration. Their capacity remains available while a primary is empty, without guaranteeing the full profile limit or autoscaler selection order.

1. Disable primary scale-up with `gcloud container node-pools update POOL --cluster CLUSTER --zone ZONE --project PROJECT --enable-autoscaling --total-min-nodes=0 --total-max-nodes=0`. These pools use total-node limits; use the matching flags. Do not resize, cordon, drain, evict, or delete busy nodes. Existing runner pods must finish naturally; idle primary nodes may still accept jobs until the autoscaler removes them.
2. Wait for zero Kubernetes nodes in that pool and verify every backing MIG has target size zero, no instances, and no pending instance actions. Check runner pods across all namespaces. If the pool still has nodes after 45 minutes, work on another profile and revisit it.
3. Change only that profile's `spot` value, render the deployment configuration, and run `tofu plan`. Apply the saved plan only when its changes are exactly the intended empty primary pool(s), restoring their original maximum. Recheck zero nodes/instances and no runner pods immediately before apply. Keep other profiles at their currently deployed setting during each batch.
4. Read back `config.spot`, machine type, and autoscaling maximum. Verify a real job on a node with `cloud.google.com/gke-spot=true`, review autoscaler selection logs, and check runner pod events for migration-caused termination. Finish with a full no-op plan.

To roll a profile back, repeat the same zero-node procedure with its previous `spot` value; its alternative pools and workflow selectors stay unchanged. If GCP rejects a T2A Spot pool in its existing zone, restore that profile to on-demand with a newly reviewed plan and report the exception without substituting a machine family. Keep Kubernetes credentials in a private `KUBECONFIG` and update changed administrator egress CIDRs through config/render/plan/apply.

The diversified example maxima require 616 N2 Spot vCPUs, 648 N2D Spot vCPUs, 164 E2 Spot vCPUs (1,428 PREEMPTIBLE_CPUS total), 864 N2D on-demand vCPUs, and 1,184 T2A on-demand vCPUs (480 primary + 704 alt1). Conservatively counting Spot against family quotas too gives N2=616, N2D=1,512, and E2=164 plus the system pool. The unchanged 176 runner nodes, 30 GB boot disks, maximum 512 GiB work disks, and four 50 GB system disks require `176 × (30 + 512) + 4 × 50 = 95,592 GB` of SSD_TOTAL_GB capacity. Check regional limits and other usage before applying; a 500-vCPU T2A limit cannot satisfy these unchanged ARM budgets. Allow headroom for asynchronous disk deletion.

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

## Protected on-demand rollout

Release 專用 profiles 使用 `targets` 限定 protected namespaces，省略時維持部署至全部 targets。設定需引用已存在的 target 名稱；未知名稱會在 render／install 前失敗。`workspace_variants_gib` 可在 profile 覆寫全域清單，`[]` 只建立無尾綴。這些欄位不傳入 OpenTofu；`node_pool_variants` 只輸出機型、架構、Spot、zone 與 pool 預算，因此 target／disk variants 不會增加 node pools。

採用 README 的四種 N2D custom shapes 時，a／b 各建立一個獨立 pool：4cpu、8cpu 各分 2+2；16cpu、32cpu 各分 3+3。八個 pools 均為 on-demand、initial=0、total_min=0，profile 合計為 20 台。新增 N2D vCPU ceiling 為 `4×8 + 4×16 + 6×32 + 6×48 = 576`。既有 N2D on-demand 864 加為 1,440；保守再計入既有 N2D Spot 648 為 2,088。N2 Spot 616 與 PREEMPTIBLE_CPUS 1,428 不變。這是 fleet 設定上限；套用前仍需讀取 us-central1 當下 quota usage／limit，納入其他服務。使用 `[128]` override 且 protected targets 預設 32Gi 時，新增 SSD ceiling 為 `20×(30+128)=3,160 GB`，既有 95,592 加為 98,752 GB；保留刪除延遲餘裕。

先量測 system pool 每節點的 allocatable、pod requests、metrics 與剩餘 pod slots，尤其注意 CPU／pod slots 分散在不同節點時不可合併使用。四種 profiles × 五個 disk variants × 兩個 targets 為 40 listeners，新增 requests 720m CPU／1,536Mi RAM；若餘裕不充足，四個 profiles 都設定 `[128]`，只新增 16 listeners、480m CPU／768Mi RAM。保留所有既有 listeners requests 與 system pool 設定，避免為此變更滾動常駐服務。

1. 備份本機設定／state 並記錄既有 pools、scale-set UIDs／specs、Helm revisions、listeners 與執行中 runner 的 metadata。檢查四個 profiles 僅引用兩個 protected targets，且所有候選 `spot: false`。不要修改既有 runner group／GitHub App／allowlist。
2. 從已通過 CI 並 merge 的 main render。先 plan 再 apply；pool 計畫只能新增八個 runner pools，不得變更 cluster、system、IAM 或既有 pools。若有他人暫時的 live drift，僅在協調過的情況下用新 pools 的明確 `-target` 儲存／檢查計畫；不要套用還原他人 drift 的全量計畫。
3. 執行 `install --add-only --plan-only`，確認只有兩個 protected namespaces 的新增 releases。檢查既有 values 差異記錄，但不升級它們；檢查固定 chart、group、OD selector、minRunners=0、明確磁碟容量，再執行 `install --add-only`。
4. 讀回八個 pools 的 spot/preemptible=false、zone、min=0、max；確認一般 targets set count 不變，新增 protected listeners 全部 Ready。比對既有 ARS UIDs／specs／Helm revisions、listener UIDs、runner restarts 與事件，追查任何 job 終止。最後執行完整 no-op OpenTofu plan 與空 additions plan。
5. 僅在 protected group 已允許且已有無副作用模式的 workflow 執行真實 job；由 job→pod→node→Compute VM 核對 profile、`cloud.google.com/gke-provisioning=standard` 與 VM scheduling。不可為了驗證擴大 group allowlist；沒有合適的現成入口時將 job proof 交給 release workflow 維護者。

回退時先讓 workflows 停用新 labels，等待新 profiles 的 runner 工作自然完成。移除這 16 個新增 Helm releases，保留 controller 與所有舊 releases；確認 OD runner pods／PVCs／磁碟與 backing MIGs 歸零後，從設定移除四個 profiles，render 並檢查只刪除八個 OD pools 的 saved plan，再 apply。不要回存舊 Terraform state，也不要 drain／evict 工作。`install` 不會自動 prune 已移除的 profiles／targets；縮小 `targets` 前也須依此處理不再部署的 releases。

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
