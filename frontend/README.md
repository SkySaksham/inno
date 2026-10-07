# Inno frontend

React landing page and reliability dashboard. React Bits' BranchedMenu is used as an in-page switcher for static signals, semantic context, suggested fixes, and all-time metrics. There is no top navbar.

## Run locally

```bash
cd frontend
npm install
npm run dev
```

Start the metrics API from `../metrics-backend/README.md`. `src/App.jsx` reads the hosted `/metrics/summary` endpoint by default. For local development or another deployment, set `METRICS_API_URL` there to the API's `/metrics/summary` endpoint, and set the backend's `FRONTEND_ORIGIN` to the exact frontend origin shown by Vite (usually `http://localhost:5173`).

The dashboard reads public aggregate metrics only. Supabase credentials and the metrics write token stay in the backend.
