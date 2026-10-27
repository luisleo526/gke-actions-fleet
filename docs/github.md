# GitHub registration and access

## Organization-wide runners

1. Create a GitHub App owned by the organization. For organization runners grant **Organization: Self-hosted runners — Read and write**. Repository metadata read access is implicit. ARC scale sets do not need a public webhook endpoint; leave webhooks disabled unless another integration needs them.
2. Install the App on the intended organization. Record the numeric App ID, installation ID, and a generated PEM private key.
3. In organization **Settings → Actions → Runner groups**, create `gke-runners`. Explicitly allow the repositories that should use it. For public repositories, enable access only after deciding how fork workflows are reviewed and isolated.
4. Set a target's `github_url` to `https://github.com/YOUR-ORG`, `runner_group` to the existing group, and `github_secret` to a Kubernetes Secret name. ARC does not create runner groups.
5. Run `runnerctl.py create-secret` for that target, then `install`. Secrets remain in the target's `arc-TARGET` namespace; runner pods cannot mount a Kubernetes API token or read the App Secret.

Group configuration and GitHub plan restrictions still apply. A URL alone does not authorize a repository. If multiple groups contain the same runner name, workflows may use `runs-on: {group: gke-runners, labels: gke-8cpu-16gb-amd64}` for explicit group selection.

## Repository registration

For a standalone repository, use `https://github.com/OWNER/REPO`, omit `runner_group`, install the App on that repository, and grant **Repository: Administration — Read and write**. Create a separate target and Secret for each installation/trust boundary. GitHub Apps work for these organization/repository targets; enterprise-wide registration and GitHub Enterprise Server are outside the current config contract.

## Multiple targets, one cluster

```json
{
  "engineering": {
    "github_url": "https://github.com/example-engineering",
    "github_secret": "engineering-app",
    "runner_group": "gke-runners"
  },
  "standalone": {
    "github_url": "https://github.com/example-owner/example-repo",
    "github_secret": "standalone-app"
  }
}
```

Use this object as `config.targets`. Both targets receive the same named profiles in different namespaces; their jobs share each profile's bounded node pool. They must belong to the same administrative trust boundary. Public names in configuration examples are illustrative.

## Credentials and OIDC

Keep App keys outside the checkout. `create-secret` accepts the key **file path**, not the key text, and applies the Secret over stdin without kubectl's last-applied annotation. Restrict Kubernetes Secret read access and use your organization's supported Secret encryption/rotation controls. `create-secret` replaces the App credential data under its field manager; rotate during a quiet window and check listener recovery. Do not print Secret YAML or copy credentials into workflow logs.

Jobs get no node service account permissions. A deployment workflow can authenticate with GitHub OIDC and an independently configured GCP/AWS federation policy. Restrict issuer, audience, repository and branch/environment claims. No deployment identities or trust policies are bundled into this generic runner project.

References: [ARC quickstart](https://docs.github.com/en/actions/tutorials/use-actions-runner-controller/quickstart), [ARC authentication](https://docs.github.com/en/actions/tutorials/use-actions-runner-controller/authenticate-to-the-api), [runner group access](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access).
