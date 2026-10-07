# Inno metrics API

Small Flask API for storing one review's false-positive and fix-acceptance counts in Supabase and fetching the latest review plus all-time rates.

The PR workflow sends review and post-fix verification counts with `publish_metrics.py`. Metrics publishing is best-effort, so an API outage does not change the PR review result.

## Setup

Run the SQL that creates `public.review_metrics` and the `public.all_time_review_metrics` view in the Supabase SQL Editor. The table needs a unique constraint on `(repository, pr_number, head_sha)` for upserts.

```bash
cd metrics-backend
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add only your Supabase project URL and service-role key. Keep the service-role key on the server; never put it in the frontend. `METRICS_API_TOKEN` is optional: when unset, API endpoints work without a bearer token; set it as a server/deployment secret to enable token checks. Set `FRONTEND_ORIGIN` in the deployment environment to your frontend's exact origin so its browser can read the public aggregate endpoint.

For a local PowerShell run, set the frontend origin in the terminal. Add an API token only if you want token checks enabled:

```powershell
$env:FRONTEND_ORIGIN = "http://localhost:5173"
# Optional:
# $env:METRICS_API_TOKEN = "your-long-random-token"
```

Then start the API:

```bash
python app.py
```

## Endpoints

When `METRICS_API_TOKEN` is set, metrics endpoints require `Authorization: Bearer <METRICS_API_TOKEN>`. When it is unset, they accept requests without a token.

### `POST /metrics`

Insert or update metrics for a repository, PR, and commit. Sending the same key again updates that row.

```json
{
  "repository": "owner/repo",
  "pr_number": 42,
  "head_sha": "abc123",
  "total_findings": 10,
  "false_positives": 2,
  "fixes_suggested": 8,
  "fixes_accepted": 5
}
```

### `GET /metrics/latest`

Returns the most recently reviewed commit and the all-time totals and rates from the `all_time_review_metrics` view.

### `GET /metrics/summary`

Public, aggregate-only metrics for the dashboard. Set `FRONTEND_ORIGIN` to the deployed frontend origin to allow its browser request. This endpoint never returns individual PR or commit details.

### `GET /` and `GET /health`

Unauthenticated health check returning `{"status":"ok","service":"inno-metrics-api"}`.
