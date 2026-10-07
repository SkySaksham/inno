# Inno metrics API

Small Flask API for storing one review's false-positive and fix-acceptance counts in Supabase and fetching the latest review plus all-time rates.

## Setup

Run the SQL that creates `public.review_metrics` and the `public.all_time_review_metrics` view in the Supabase SQL Editor. The table needs a unique constraint on `(repository, pr_number, head_sha)` for upserts.

```bash
cd metrics-backend
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set the Supabase project URL, service-role key, and a long random `METRICS_API_TOKEN`. Keep both secrets on the server. Start the API with:

```bash
python app.py
```

## Endpoints

All metrics endpoints require `Authorization: Bearer <METRICS_API_TOKEN>`.

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

### `GET /health`

Unauthenticated health check.
