# Only organization members may run PR workloads on GKE

Copy [examples/member-only-pr.yml](../examples/member-only-pr.yml) to `.github/workflows/gke-member-pr.yml` on the repository's trusted base branch. This template uses `pull_request_target` so the admission logic comes from the base branch, never from a PR-modified workflow. The admission job runs on a GitHub-hosted runner and does not check out or execute any PR code. The GKE job starts only after admission succeeds.

## Identity setup

Create a GitHub environment named **gke-pr-admission** in each integrating repository. Set its deployment branch policy to **Selected branches**, permitting only the protected trusted base branch (`main` in the example). Store the private key only as an environment secret; do not duplicate it in repository/organization secrets. This prevents an untrusted base branch from loading the identity credential. The template also filters PR target branches to `main`; for `dev`/`master`, change the trigger, environment branch policy and runner-group workflow ref together.

Create a **separate** organization-owned GitHub App with **Organization permissions → Members: read**, install it on that organization, and set these values:

| Setting | Type | Value |
|---|---|---|
| `GKE_MEMBERSHIP_APP_ID` | Actions variable | Numeric App ID |
| `GKE_MEMBERSHIP_APP_PRIVATE_KEY` | gke-pr-admission environment secret | PEM private key for the membership-only App |

The supplied runner management Apps have runner administration permissions; they are not suitable PR identity credentials. Their keys stay in Kubernetes and are never passed to this workflow. Ordinary repository `GITHUB_TOKEN` cannot reliably query private organization memberships. `author_association` alone is not used as the final authorization decision.

The template issues a short-lived installation token restricted to Members: read and checks **current active membership** for the PR author, the event actor, and the actor requesting a rerun. Public and private organization memberships work. External collaborators, nonmembers, pending invitations and bots without membership are denied. Missing credentials, permission failures, rate limits and API failures fail closed. The membership token is not exported as a job output or passed to the GKE test job.

An external PR can create a small GitHub-hosted admission run/record; GitHub's PR event filters do not filter organization membership before workflow creation. It does **not** get a GKE runner when admission fails. If you require no workflow record at all, an external event broker is needed; that is a different trigger design.

## Enforce the trusted entry point

The `if:` check is not sufficient by itself: a malicious PR could introduce a new workflow that directly requests a runner name. In organization **Settings → Actions → Runner groups → gke-runners**, configure both:

1. Repository access: only repositories that have completed integration.
2. Workflow access: **Selected workflows**, restricted to the trusted full workflow path and base ref, for example `YOUR-ORG/YOUR-REPO/.github/workflows/gke-member-pr.yml@refs/heads/main`.

For a `dev` or `master` base branch, list that exact ref instead. Authorize only the intended protected branches. Keep workflow access restrictions enabled; granting repository access without restricting workflows defeats the admission boundary. If your GitHub plan does not support selected-workflow restrictions, do not claim this boundary is enforced; use an appropriate plan or a separate authenticated dispatch service.

Protect the base branch and require trusted review for `.github/workflows/**` changes, for example through CODEOWNERS plus branch protection/rulesets. Keep the App key only in the protected admission environment, with its selected-branch policy enabled; the GKE worker job must not reference that environment. The template gives the GKE job only `contents: read`, does not inherit secrets, checks out the immutable admitted head SHA, and disables checkout credential persistence. Avoid adding `secrets: inherit`, privileged deployment credentials or write permissions to the test job. Running PR code with `pull_request_target` is permitted here only after the membership gate, through the restricted trusted workflow.

The `runs-on` labels choose a profile/architecture, not an authentication identity. GitHub App registration, runner-group repository/workflow restrictions, and the admission check are separate controls. GCP ADC is used only by the infrastructure operator; workload cloud access must use a separately restricted GitHub OIDC federation configuration.

## Acceptance checks

Before moving production repositories, test an active member PR, a nonmember/fork PR, a pending or removed membership, a nonmember rerun, API failure/missing App credentials, and a PR that attempts to add a direct `runs-on: gke-...` job. Only the active-member path may reach GKE. Keep the base workflow and runner-group allowlist identities in the receipt. Existing repository workflows and organization policies are not modified by merely copying this example.
