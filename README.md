# HROne — Employee Attendance & Analytics API

FastAPI + MongoDB service implementing the full assignment contract:
attendance CRUD with business rules R1–R5, concurrency-safe writes, analytics
(window functions, densified trends), and an explain endpoint.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# MongoDB 6+/7 required — local docker: docker run -p 27017:27017 mongo:7
# create .env with MONGO_URI and MONGO_DB
python sample_seed.py
uvicorn app.main:app --port 8000
```

## Design highlights

- Timezone-aware datetimes throughout; IST date logic including overnight shifts (R1)
- Late minutes: strict 10-minute grace on raw seconds; work hours: half-up 2-decimal; overtime: 30-minute floor
- Race-safe writes: unique `(emp_code, date)` index + `DuplicateKeyError` → 409 (no check-then-insert)
- PATCH corrections append history entries; optimistic concurrency → 409; no-op → 422
- Analytics as aggregation pipelines: department summary starts from employees (zero-log headcount),
  leaderboard ranks with `$setWindowFields` + `$rank` (1,2,2,4), trend densifies the calendar and
  computes a 7-day moving average
- `/admin/explain/*` runs `executionStats` on the same queries/pipelines the endpoints build
- Unique indexes created at startup

## API

| Method | Path | Purpose |
|---|---|---|
| GET | /health | liveness + Mongo reachability |
| POST/GET | /employees | create / list (filter, sort, pagination) |
| POST | /attendance/punch-in | clock in (R1 date, derived fields) |
| POST | /attendance/punch-out | clock out (work hours, overtime, half-day) |
| PATCH | /attendance/{emp_code}/{date} | manual correction with history |
| GET | /attendance | list (filters, stable sort, pagination) |
| GET | /analytics/employees/{emp_code}/monthly | monthly summary |
| GET | /analytics/departments/summary | per-department summary |
| GET | /analytics/leaderboard/late | ranked late totals |
| GET | /analytics/departments/{dept}/trend | daily trend with moving average |
| GET | /admin/explain/{endpoint} | executionStats for the real query plans |

## Notes

- All instants are stored as UTC in MongoDB and returned as epoch milliseconds.
- Seeded records may omit `history`/`half_day`; the API treats them as `[]`/`false`.
