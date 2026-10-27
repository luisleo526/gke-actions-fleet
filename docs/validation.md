# Validation status

Local validation completed on 2026-10-27:

| Check | Result | Scope |
|---|---|---|
| Python contract tests | 42 passed | Sizes, architecture/machine pairing, resource headroom, credential-free rendering, isolated scheduling, configurable 480 GiB ephemeral storage, VM/disk cleanup predicates |
| Ruff lint and format | Passed | Scripts and tests, including the actual inline membership gate executed with mocked API outcomes |
| OpenTofu fmt / validate | Passed | Google provider 7.46.2, OpenTofu 1.12.6 |
| OpenTofu mock-provider plan | 2 passed | One cluster, mixed architecture pools, zero minimums, system pool, private nodes, taints and Workload Identity |
| Official Helm chart render + ARC CRD schema | 20 passed | All 10 profiles with Kubernetes 1.32 and 1.35 template capabilities, ARC 0.15.1, verified chart archive checksums |
| Workflow actionlint + shellcheck | Passed | Repository CI and all three example workflows |
| Upstream runner / DinD manifest inspection | Passed | Pinned multi-platform index digests contain linux/amd64 and linux/arm64 |
| Optional toolchain image build | Passed on AMD64 and ARM64 | Native GitHub Actions builds on deployed GKE runners; image not published |
| GCP infrastructure apply | Applied and read back | One GKE 1.36.4 cluster, 20 worker pools, one system pool, two GitHub organizations |
| Live GitHub/GKE execution | 36 jobs passed across 10 workflow runs | All 10 profiles, a second organization, representative alternative machine/zone pools, native toolchain image builds, and a final native Docker test committing 3 GiB on both architectures |
| Idle cleanup | Passed | Zero runner nodes/VMs, work PVCs/PVs/disks, ephemeral runner resources and both groups’ GitHub runner registrations; only the system node/boot disk remains |
| Full configured concurrency | Not established | The 136-job configuration was not tested at full load; confirm project-specific regional quotas and available machine capacity |

Reproduce static checks with `make setup`, `make test`, and `make charts`. Helm schemas validate ARC resource structure, not all runtime Kubernetes/GKE behavior. The mock plan makes no provider API calls and does not prove regional machine availability, quotas, scheduling from zero, disk provisioning/deletion or GitHub registration. The optional image has passed native builds and tool checks on both platforms; repository-specific runtime and browser dependencies still need migration canaries.

Operator acceptance steps are in [operations.md](operations.md). Private local workflow inventories and generated chart receipts live in ignored `build/` and are not part of this public source tree.

Membership admission is covered by local tests for active members, nonmembers, pending invitations, event/rerun actors, API failures, no PR checkout in the gate and no credential inheritance. A real Members: read App must still be configured when integrating that template; no live member/nonmember PR acceptance test is claimed. The deployed canary groups restrict access to selected repositories and trusted workflow refs.

Output-isolation and project-identity regression checks passed: chart renders use build/validation/ without modifying deployment parameters, and the state-bound project identity rejects a wrong-project plan before apply. Private rollout details, quota request results and recovery receipts remain in ignored operator output.

Final Docker budget correction: DinD CPU/memory limits scale with each profile instead of a fixed 2 GiB ceiling. Thirty local tests cover the generated limits across all profiles. After this change, two additional native GitHub jobs each committed 3 GiB within Docker successfully. The previous 34-job suite covers the unchanged runner resource/storage/architecture contracts and image builds; the final resource change has the scoped two-job regression evidence.

## Standard runner and target follow-up

Six additional native jobs passed across AMD64/ARM64, verifying repository/environment secrets and variables, repository GITHUB_TOKEN access, environment-bound OIDC, reusable workflow secret inheritance, sudo/apt, a writable-layer allocation over 2 GiB, Docker builds, job/service containers, and artifact/cache restoration across fresh jobs. Synthetic credentials were used; no production secrets were read or changed. An initial canary assertion expected a legacy OIDC subject; the corrected probe verified immutable repository/owner IDs and the actual GitHub subject format without changing any OIDC policy.

Two further native jobs passed with a target-specific private toolchain image and distinct probe scale-set names. The probe used a temporary dedicated GitHub runner group, preserving the separately managed general/protected groups. Three configured target namespaces each have ten ready listeners; protected names and per-target image digests were read back from the live runner templates. This does not claim execution of a production credentialed release workflow or a dedicated-host isolation boundary.
