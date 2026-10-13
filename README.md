# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
# set MONGO_URI and MONGO_DB (environment variables or a local .env)
uvicorn app.main:app --port 8000
```
Indexes are created at startup. `GET /health` returns 200 when MongoDB is reachable. Interactive docs: `/docs`.

## What is done
- Part A: review of the starter, 19 defects in `REVIEW.md`.
- Part B: punch-out, PATCH regularization with audit history, and the four analytics endpoints (aggregation pipelines).
- Part C: `/admin/explain/{endpoint}` and `DECISIONS.md`.

## How it was tested
Manually through `/docs` on a local MongoDB 7 with a small dataset: punch-in/out (including 409s), regularization
(including no-op 422 and 404), employee monthly, department summary (including an employee with no logs),
late leaderboard (including a tie and the department filter), department trend (weekends, moving average,
404/422 cases), and `/admin/explain` (IXSCAN and no COLLSCAN on all five).

## Not tested / known gaps
- Large datasets and heavy parallel requests.
- Overnight-shift data through the API (the date and overtime logic was only checked with unit-style examples).

## Notes
- Rounding is half-up (done in Decimal128 in the pipelines, since MongoDB's `$round` rounds ties to even).
- `moving_avg_7d` averages the reported (4-decimal) `attendance_rate` values.