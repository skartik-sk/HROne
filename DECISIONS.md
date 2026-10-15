# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
ans :  unique emp_code, unique (emp_code, date) (also the race guard), (date, emp_code) for list queries, department for filters. Rejected: status-only index (low selectivity), text index (no search requirement).



2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
ans: find-then-insert is racy under concurrency, the unique compound index is the real guard — insert and catch DuplicateKeyError  409.



3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
ans: $setWindowFields + $rank for standard competition ranking (1,2,2,4), ordered total desc, emp_code asc, limit keeps all rows with rank ≤ limit.


4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
ans: summary pipeline starts from employees, $lookup logs — zero-log employees still count.

5. **One thing you would change** if this had to serve 100x the data.
ans: monthly partitions/time-series, pre-aggregated rollups, covering indexes, explain endpoint proves IXSCAN/no COLLSCAN.

