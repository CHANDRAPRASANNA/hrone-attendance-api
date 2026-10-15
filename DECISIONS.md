# DECISIONS.md

1. **Indexes.** On `employees`: unique `emp_code` (lookups, and it rejects duplicate creates), `(department, emp_code)` for the filtered, sorted list and the trend, and `joined_on` for headcount. On `attendance_logs`: unique `(emp_code, date)` (one record per day; also serves per-employee queries and every `$lookup`), `(date desc, emp_code)` matching the list sort and the month ranges, and `(status, date, emp_code)` for status filters. I rejected an index on `late_minutes`: the month range already narrows the leaderboard scan.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one` without checking first. The unique index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which becomes 409. There is no gap between a read and a write to race in.

3. **Ties.** `$rank` runs over every employee before any limit, so tied totals share a rank and the next rank is skipped. Rows are ordered by rank, then `emp_code`, and `$limit` cuts after that, so a tie at the cutoff returns exactly `limit` rows and drops the later `emp_code`s.

4. **Headcount.** The pipeline starts from `employees` filtered on `joined_on <= month end` and looks the logs up. An employee with no logs keeps a row with an empty array, so they are counted.

5. **At 100x.** Maintain a per-employee monthly summary collection, updated on punch-out and correction, so analytics read thousands of rows instead of millions.
