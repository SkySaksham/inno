# Inno

Inno reviews Python pull requests with Pylint, Bandit, and GitHub Copilot CLI. It adds semantic checks for logic and caller-contract issues, posts evidence and one-click fixes where possible, and blocks merges on high or critical findings. Review metrics are sent to the Inno dashboard backend.

## Add Inno to a repository

Copy these template files into `.github/workflows/` in the repository you want reviewed:

```text
Inno source                  Target repository
workflows/pr-review.yml  ->  .github/workflows/pr-review.yml
workflows/inno.yml       ->  .github/workflows/inno.yml
```

`pr-review.yml` runs on pull requests targeting `main`. `inno.yml` is optional; it maintains the `metadata-branch` used to add repository context to reviews. Without it, Inno still runs with AST-only context. Change the workflow branch filters if your default branch is not `main`.

## Connect GitHub Copilot

1. Create a fine-grained personal access token with the **Copilot Requests** permission enabled.
2. In the target repository, open **Settings → Secrets and variables → Actions → New repository secret**. Name it `COPILOT_TOKEN` and paste the token.
3. Set the Actions variable `INNO_LLM_BACKEND` to `cmd`, or leave it unset (`cmd` is the default).

The workflow installs Copilot CLI and uses the secret for its AI reviews. Keep the token in Actions secrets; do not put it in workflow files. GitHub documents this token permission and Actions setup in [Copilot CLI authentication](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/automate-with-actions).

In **Settings → Actions → General**, allow workflows to write repository contents and pull-request comments so the metadata and review workflows can update their results.

## Metrics

The PR workflow sends finding, false-positive, and fix-acceptance counts to the hosted metrics backend. The frontend dashboard is in [`frontend/`](frontend/README.md); backend setup is in [`metrics-backend/`](metrics-backend/README.md).
