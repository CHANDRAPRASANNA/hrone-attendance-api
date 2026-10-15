# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne engineering assignment. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb+srv://..."   # or put MONGO_URI / MONGO_DB in a local .env
export MONGO_DB="hrone"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are required; real environment variables take priority over `.env`.
Indexes are created idempotently at startup. MongoDB 6.0+ is required (developed against MongoDB Atlas).

## Notes

- Instants in requests and responses are epoch milliseconds; MongoDB stores UTC datetimes.
- `GET /admin/explain/{endpoint}` accepts the same query parameters as the endpoint it explains
  (`emp_code`, `department` and the rest are passed as query parameters there).
- Analytics are single aggregation pipelines; Python only formats the returned rows (half-up rounding).
- If MongoDB is unreachable, endpoints return 503 rather than 500.
