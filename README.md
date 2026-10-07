# Inno

Inno reviews pull requests for Python issues using Pylint, Bandit, and an LLM. It posts a review summary, adds GitHub one-click suggestions when a safe fix maps to changed lines, and can block merges for high or critical findings.

## GitHub Actions setup

Copy `pr-review.yml` and `inno.yml` into `.github/workflows/` in the repository you want Inno to review. The PR workflow currently targets `main`; edit its `pull_request.branches` setting to change that.

Configure repository settings:

- Allow GitHub Actions to write pull request comments (`pull-requests: write` is requested by the workflow).
- Set the `INNO_LLM_BACKEND` Actions variable to `cmd`, `openai`, or `gemini` (defaults to `cmd`).
- For `cmd`, add a `COPILOT_TOKEN` secret with Copilot CLI access. For `openai` or `gemini`, add `OPENAI_API_KEY` or `GEMINI_API_KEY` instead.

### Workflows

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `pr-review.yml` | Pull requests targeting `main` | Scans changed Python code with Pylint and Bandit; runs AI and semantic reviews; merges and posts findings and applicable one-click suggestions. High and critical blocking findings fail the job. On subsequent PR updates, it verifies which findings were fixed. If verification fails, it reruns AI and semantic review on the current PR head and posts fresh suggestions; the check remains failed until blockers are fixed. Results are uploaded as the `inno-results` artifact. |
| `inno.yml` | Pushes to `main` | Updates Python code metadata on a separate branch. It processes changed Python files incrementally, or builds metadata for the whole repository if its metadata branch does not exist. |

The workflow files currently use different metadata branch names: `inno.yml` writes to `metadata-branch`, while `pr-review.yml` reads from `inno-metadata`. Set them to the same name if you want PR reviews to use the generated metadata; without metadata, review continues with AST-only context.

## Local use

Use Python 3.11 or newer. Install the static analyzers:

```bash
python -m pip install pylint bandit
```

Choose an LLM backend by setting `INNO_LLM_BACKEND` and its credential. For example, with Copilot CLI installed and authenticated:

```bash
export INNO_LLM_BACKEND=cmd
export INNO_LLM_CMD="copilot -p"
```

Run the PR-review pipeline from the repository being reviewed (replace `origin/main` if needed):

```bash
python scan.py --base origin/main --head HEAD --out findings.json
python context.py --findings findings.json --metadata metadata --out payloads.json
python ai_review.py --payloads payloads.json --out review.json
python semantic_context.py --repo . --base origin/main --head HEAD --metadata metadata --out semantic_payloads.json
python semantic_review.py --payloads semantic_payloads.json --out semantic_findings.json
python merge.py --review review.json --semantic semantic_findings.json --out combined_review.json
python report.py --review combined_review.json --fail-on high --print
```

`report.py` prints the review locally. It posts a comment and native suggestions when run in a GitHub pull request with the required token and repository environment variables. `--fail-on` accepts `critical`, `high`, `medium`, or `low`.

To update metadata locally, run `init.py --repo . --script extract_metadata.py`. It uses the repository's configured Git remote and pushes the generated metadata branch, so only run it when you intend to publish that update.

## Metrics dashboard

The React landing page and all-time reliability dashboard are in [`frontend/`](frontend/README.md). It uses the BranchedMenu component to switch between Inno's review approach and reliability metrics, without a top navbar. Its Flask/Supabase API is in [`metrics-backend/`](metrics-backend/README.md).
