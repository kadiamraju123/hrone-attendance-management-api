# REVIEW.md

The starter's basic happy path was not sufficient for the contract in `openapi.yaml`. Main defects identified and addressed in the implementation:

| # | Where in starter | What was wrong | How to notice | How it was addressed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It replaced the punch-in date with the shift-start time, so the elapsed-time calculation could be wrong; it also ignored the attendance date and overnight shifts. | Test a punch-in on a different date or just over the 10-minute grace period. | Compare the actual instant with the shift start for the record's attendance date, in IST, and apply the strict 10-minute grace rule. |
| 2 | `compute_work_hours` | Built-in `round` does not guarantee the contract's half-up rounding. | Test a duration that falls exactly on a half-centesimal rounding boundary. | Use `Decimal` with `ROUND_HALF_UP`, after truncating instants to whole seconds. |
| 3 | `compute_overtime` | It ignored overnight shifts and did not apply the 30-minute minimum. | Test a night shift and punch-outs 29 and 30 minutes after shift end. | Calculate the correct shift end in IST, then apply the 30-minute threshold. |
| 4 | `EmployeeIn` | The starter did not validate employee-code format, email, dates, shift times, or differing shift start/end. | Submit malformed values or identical shift times. | Added Pydantic validation and date/time checks. |
| 5 | `GET /health` | It always returned success even when MongoDB was unavailable. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 if it is unavailable. |
| 6 | `POST /employees` | Duplicate detection was a read-before-write race and no unique index was created. | Send simultaneous requests with the same `emp_code`. | Create a unique index and translate duplicate-key errors to 409. |
| 7 | `GET /employees` | `skip` used `page * page_size`, totals ignored the department filter, and pagination had no bounds or stable sort. | Compare page 1/page 2 and filtered totals. | Use `(page - 1) * page_size`, filtered count, stable sorting, and page-size validation. |
| 8 | `POST /attendance/punch-in` | Missing employee could cause a server error; timestamp units/timezone, status, attendance date, and duplicate concurrency were not safely handled. | Try unknown employee, invalid timestamp, overnight shift, or simultaneous duplicate requests. | Validate epoch milliseconds, derive the IST attendance date, validate statuses, and rely on a unique compound index. |
| 9 | `GET /attendance` | Loaded and sorted the entire collection in Python, returned internal Mongo IDs as API `id`s, and lacked input validation. | Test filters on a large dataset and inspect the response contract. | Move filtering, sorting, count, skip, and limit to MongoDB; remove `_id` from API responses and validate dates. |
| 10 | Starter endpoint set | Punch-out, regularization/history, four analytics endpoints, and explain-plan endpoint were absent. | Compare registered routes with `openapi.yaml`. | Implemented the missing routes and associated validations/aggregation logic. |

## Reviewed but not treated as defects

- MongoDB stores instants as BSON datetimes while the API exposes epoch milliseconds; this is an intentional API/storage boundary, not a contradiction.
- `history` is reserved for manual corrections only. Normal punch-in/punch-out operations should not append audit entries.
- The supplied sample data demonstrates document shape, not complete edge-case coverage; passing sample checks alone does not prove correctness on the larger evaluation dataset.

## Verification status

The candidate reported that each route was exercised through Swagger and no HTTP errors were encountered on those tests. Static Python compilation is also run before packaging. Full MongoDB integration, race-condition tests, exhaustive rule-based analytics checks, and `executionStats` inspection against a large dataset still need to be confirmed in the candidate's local MongoDB environment before final submission.
