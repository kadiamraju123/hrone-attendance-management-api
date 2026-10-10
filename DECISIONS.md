# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index prevents duplicate employees; `(department, joined_on)` helps department headcount queries. A unique `(emp_code, date)` attendance index enforces one daily record per employee. `(date DESC, emp_code ASC)` supports attendance-list ordering, `(emp_code, punch_in DESC)` supports locating a recent punch, and `(date, status, late_minutes)` supports attendance analytics. I did not add an index for every optional filter because excess indexes increase write cost and storage.

2. **Punch-in race.** Both requests may check the employee and prepare a record. The unique `(emp_code, date)` index allows only one insert; MongoDB rejects the other with `DuplicateKeyError`, which the API returns as HTTP 409.

3. **Ties.** The leaderboard uses MongoDB `$rank` on total late minutes. The limit is applied to rank, not row position, so every employee tied at a rank within the limit is returned; rows are then ordered by minutes and employee code.

4. **Headcount.** Department summary starts from employees who joined by month-end, then looks up attendance. Employees with no logs remain in the pipeline. A per-employee grouping prevents multiple daily records from inflating headcount.

5. **100x data.** I would benchmark the real query plans and latency, then refine compound indexes and move any remaining per-row analytics work into aggregation/window stages. I would also add automated integration and concurrency tests against a production-sized dataset.
