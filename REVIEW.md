# REVIEW.md

Review of the starter `app/main.py`. Fixes are in my `app/main.py`; function names below refer to it.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware=True`, so PyMongo returns naive datetimes. Any conversion to epoch ms or IST then uses the server's local zone. | Response times shift by 5.5 h on a machine not set to UTC. | `tz_aware=True`; `dt_to_ms` also treats a naive value as UTC. |
| 2 | `/health` | Always returns ok, never pings MongoDB. | Stop MongoDB: health still says 200; the grader starts testing too early. | Pings Mongo, 503 on failure. |
| 3 | module level | No indexes created at startup, and none unique. `/attendance` and analytics scan the whole collection; uniqueness is not enforced. | `explain` shows COLLSCAN; duplicate rows under parallel requests. | `ensure_indexes()` in the lifespan, idempotent, named so queries can hint them. |
| 4 | `create_employee` | Check-then-insert race: two parallel creates both pass `find_one` and both insert. | 20 parallel POSTs of one emp_code give several 201s. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 5 | `create_employee` | `created_at = datetime.now()` is naive local time, returned as a datetime rather than epoch ms. | `created_at` is a string like `2026-07-06T09:00:00` instead of an integer. | UTC time, returned as epoch ms. |
| 6 | `EmployeeIn` | No validation: any `emp_code`, email, shift format, impossible `joined_on`, and `shift_start == shift_end` are accepted. | POST `{"emp_code":"x","joined_on":"2026-02-30","shift_start":"9"}` returns 201, not 422. | Patterns, real-date check and `shift_start != shift_end` validator. |
| 7 | `list_employees` | `skip = page * page_size` skips the first page (page 1 returns rows 21-40). | Page 1 with 5 employees returns nothing. | `(page - 1) * page_size`. |
| 8 | `list_employees` | `total` counts the whole collection, not the filtered set. No sort. `page`/`page_size` unbounded. | `?department=X` with 3 matches reports total 500. Order is arbitrary. | Count with the same filter, sort by `emp_code`, `page >= 1`, `page_size` 1-100. |
| 9 | `compute_late_minutes` | Uses `punch_in.replace(hour=...)` in whatever zone the datetime has, not IST. Truncates to minutes before comparing, so 09:40:30 gives 10 and returns 0 although it is late. No overnight logic, seconds not truncated. | Shift 09:30, punch 09:40:30 returns 0, expected 10. | `calc_late`: IST shift start on the attendance date, `diff > 600 s`, then floor to minutes. |
| 10 | `compute_work_hours` | Python `round()` on a float: banker's rounding plus float error, not half-up. | 4.495 h is stored as 4.49; contract says 4.50 and not a half day. | `Decimal(seconds)/3600`, `ROUND_HALF_UP`. |
| 11 | `compute_overtime` | Counts every minute after shift end (no 30-minute minimum). Ignores overnight shifts (end is next day). `fromisoformat(...)` of a date string is fragile. | 10 min after shift end gives 10, expected 0. 22:00-06:00 shift gives huge or negative values. | `calc_close`: shift end on the right day (+1 if overnight), counted only if >= 30 min. |
| 12 | `punch_in` | Unknown employee: `emp` is None, `emp["shift_start"]` raises TypeError -> 500. | POST with `EMP9999` returns 500, expected 404. | `get_employee()` raises 404. |
| 13 | `punch_in` | `datetime.fromtimestamp(ms/1000)` is naive server-local time. `date` comes from the server's local date, not IST, and overnight shifts (R1) are ignored. Milliseconds are not truncated. | Punch 23:30 IST on a UTC server lands on the wrong date. Overnight punch-in at 02:00 gets today's date. | UTC datetime truncated to the second; `attendance_date()` converts to IST and applies the overnight rule. |
| 14 | `punch_in` | Check-then-insert race, same as #4. | Parallel punch-ins give several 201s and duplicate (emp_code, date) rows. | Unique `(emp_code, date)` index; `DuplicateKeyError` -> 409. |
| 15 | `PunchInIn` | `punched_at: Optional[int]` accepts seconds, negative or absurd numbers, and `status` is a free string (accepts ABSENT, LEAVE, "banana"). | `punched_at: 1783312500` (seconds) returns 201 in 1970. `status: "banana"` returns 201. | Strict int in range 1e11..4.1e12 (floats/strings/bools -> 422); status limited to PRESENT/WFH/ON_DUTY. |
| 16 | `punch_in`, `list_attendance` | Response contains `id` (the `_id`) and raw datetimes instead of epoch ms. Contract: `_id` never appears; instants are ms. | `id` in the JSON; `punch_in` is an ISO string. | `record_out()` shapes every response, converts instants to ms. |
| 17 | `list_attendance` | `find(q)` loads every match into Python and sorts there; sorts by `date` only (no `emp_code` tiebreak); pagination done in memory. Unusable at 100k docs. | Slow, memory-heavy; `explain` shows COLLSCAN; order of same-date rows unstable. | Database-side `sort(date desc, emp_code asc)`, `skip/limit`, covered by indexes. |
| 18 | `list_attendance` | No validation: bad dates, `date_from > date_to`, unknown `status`, `page=0` (negative slice) all accepted. | `?date_from=2026-13-45` returns 200; `?page=0` returns wrong rows. | Date parsing, range check, status enum, `page >= 1`, `page_size <= 100` -> 422. |
| 19 | `list_attendance` | `total` is `len(docs)` of the whole match: fine for correctness, but it forces loading everything. | Same as #17. | `count_documents` with the same filter (`estimated_document_count` when unfiltered). |

## Looked at and decided it is fine
- `load_dotenv()` defaults to `override=False`, so real environment variables already win over `.env`.
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct; I reused it.
- `status_code=201` on the two POST endpoints matches the contract.
- Default `mongodb://localhost:27017` is only a local-development fallback; the code reads `MONGO_URI` first.