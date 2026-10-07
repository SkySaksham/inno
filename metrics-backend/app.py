"""Small API for writing and reading Inno review metrics."""
import hmac
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from supabase import Client, create_client

load_dotenv()

app = Flask(__name__)

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
api_token = os.environ.get("METRICS_API_TOKEN")
frontend_origin = os.environ.get("FRONTEND_ORIGIN", "").rstrip("/")

if not supabase_url or not supabase_key:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
supabase: Client = create_client(supabase_url, supabase_key)


@app.after_request
def add_frontend_cors(response):
    origin = request.headers.get("Origin", "").rstrip("/")
    if frontend_origin and origin == frontend_origin:
        response.headers["Access-Control-Allow-Origin"] = frontend_origin
        response.headers["Vary"] = "Origin"
    return response


def authorized():
    # Token checks are opt-in for now; setting METRICS_API_TOKEN enables them.
    if not api_token:
        return True
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied, api_token)


def nonnegative_int(data, key):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


@app.get("/health")
def health():
    return health_response()


@app.get("/")
def root_health():
    return health_response()


def health_response():
    return jsonify({"status": "ok", "service": "inno-metrics-api"})


@app.post("/metrics")
def update_metrics():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    repository = data.get("repository")
    head_sha = data.get("head_sha")
    pr_number = data.get("pr_number")
    if not isinstance(repository, str) or not repository.strip():
        return jsonify({"error": "repository is required"}), 400
    if not isinstance(head_sha, str) or not head_sha.strip():
        return jsonify({"error": "head_sha is required"}), 400
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return jsonify({"error": "pr_number must be a positive integer"}), 400

    try:
        total = nonnegative_int(data, "total_findings")
        false_positives = nonnegative_int(data, "false_positives")
        suggested = nonnegative_int(data, "fixes_suggested")
        accepted = nonnegative_int(data, "fixes_accepted")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if false_positives > total:
        return jsonify({"error": "false_positives cannot exceed total_findings"}), 400
    if accepted > suggested:
        return jsonify({"error": "fixes_accepted cannot exceed fixes_suggested"}), 400

    row = {
        "repository": repository.strip(),
        "pr_number": pr_number,
        "head_sha": head_sha.strip(),
        "total_findings": total,
        "false_positives": false_positives,
        "fixes_suggested": suggested,
        "fixes_accepted": accepted,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        result = (supabase.table("review_metrics")
                  .upsert(row, on_conflict="repository,pr_number,head_sha")
                  .execute())
    except Exception:
        app.logger.exception("Failed to write review metrics")
        return jsonify({"error": "could not save metrics"}), 500

    return jsonify({"saved": True, "metrics": result.data[0] if result.data else row}), 200


@app.get("/metrics/latest")
def latest_metrics():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401

    try:
        latest = (supabase.table("review_metrics")
                  .select("*")
                  .order("reviewed_at", desc=True)
                  .limit(1)
                  .execute())
        summary = supabase.table("all_time_review_metrics").select("*").execute()
    except Exception:
        app.logger.exception("Failed to fetch review metrics")
        return jsonify({"error": "could not fetch metrics"}), 500

    return jsonify({
        "latest_review": latest.data[0] if latest.data else None,
        "all_time": summary.data[0] if summary.data else None,
    })


@app.get("/metrics/summary")
def public_metrics_summary():
    """Expose only aggregate reliability metrics for the public dashboard."""
    try:
        result = supabase.table("all_time_review_metrics").select("*").execute()
    except Exception:
        app.logger.exception("Failed to fetch public metrics summary")
        return jsonify({"error": "could not fetch metrics"}), 500
    return jsonify(result.data[0] if result.data else {})


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "5000")), debug=False)
