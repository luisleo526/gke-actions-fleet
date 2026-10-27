# Security scope

This project provisions ephemeral GitHub Actions runners for repositories within
one administrative trust boundary. It is not a hostile multi-tenant execution
service. The default Docker mode uses a privileged Docker-in-Docker sidecar;
namespace separation, NetworkPolicy and one concurrent runner per node do not
provide a VM-grade boundary against malicious job code.

## Credentials and publication

- Keep GCP ADC/service-account files and GitHub App private keys outside the
  repository. The CLI sends App credentials to Kubernetes over stdin; it does
  not put them in Helm values or Terraform variables.
- Do not publish local `config.json`, environment files, Terraform state/plans,
  generated `build/` output or deployment receipts. Git ignores these paths, but
  uploading or archiving the entire workspace bypasses Git's ignore rules.
- Publish only reviewed source files. Run a secret scanner such as Gitleaks on
  the exact source tree/archive, and enable GitHub secret scanning and push
  protection when creating the public repository.
- Treat Terraform state and backups as private operational data and restrict
  their filesystem/backend access. Ignore rules do not protect files already
  tracked by Git or override an explicit `git add -f`.

## Runner authorization

Restrict runner groups to selected repositories and trusted workflow refs.
For member-only PR execution, follow
[the complete member-PR setup](docs/member-only-pr.md): a separate Members: read
App, a protected admission environment/branch, live membership checks, and
read-only worker credentials. Copying a YAML file alone does not configure these
organization controls. External/unreviewed fork code must not directly request
these privileged runners.

Runner registration credentials and cloud deployment identities are separate.
Jobs receive no workload GCP role by default. Use narrowly scoped GitHub OIDC
federation for workflows that need cloud access; do not mount the operator's ADC
or runner-management App key into a job.

## Reporting and maintenance

Report sensitive findings using
[GitHub private vulnerability reporting](https://github.com/luisleo526/gke-actions-fleet/security/advisories/new)
or to the deployment operator privately. Do not put credentials, JIT registration
material or private deployment state in a public issue.

Follow upstream GitHub runner support deadlines and review ARC, image and action
updates. Verify immutable image/chart identities and run the documented canaries
after security-relevant changes. Local unit/secret scans are scoped checks, not
an independent penetration test or a guarantee that every deployment is secure.
