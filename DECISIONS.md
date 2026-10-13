# DECISIONS.md

1. **Indexes.** On `employees` I made a unique index on `emp_code` (no duplicate employees, also used for sorting and joins), one on `(department, joined_on)` for department filters and headcount, and one on `joined_on` for headcount without a department. On `attendance_logs` I made a unique `(emp_code, date)` (one record per day), `(date desc, emp_code)` for date ranges, listing and the leaderboard, `(status, date desc, emp_code)` for status filters, and `(emp_code, punch_in desc)` for punch-out. I did not add an index on `late_minutes`, because the date index already narrows the data to one month.

2. **Punch-in race.** Both requests work out the same `(emp_code, date)` and call `insert_one`. The unique index lets only one insert succeed. The other gets a `DuplicateKeyError`, which I return as 409. I do not check first and then insert, because both requests could pass the check.

3. **Ties.** I rank first with `$rank`, then keep rows with `rank <= limit`. So everyone tied at the cutoff is returned, even if that is more rows than `limit`. Dropping one of them would be unfair.

4. **Headcount.** The summary starts from `employees` and joins the logs. The `$unwind` uses `preserveNullAndEmptyArrays`, so an employee with zero logs is still kept and counted.

5. **100x data.** I would store a monthly summary per employee, updated on every punch or correction, so analytics read small summary documents instead of scanning all the raw logs.