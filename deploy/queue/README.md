# GPU job queue

A PostgreSQL 16 job queue for long-running GPU jobs (1-30 min), in plain SQL.
Workers claim jobs with `claim_job`, renew leases with `heartbeat`, and finish with `complete_job` or `fail_job`.
`reap_expired` requeues or fails jobs whose lease ran out, and clears the token so stale workers are fenced off.
`schema.sql` installs into the current `search_path` and is safe to re-run.

Run the tests against any database you can create schemas in (each run uses a throwaway schema):

    pip install -r requirements.txt
    DATABASE_URL=postgresql://user:pass@localhost:5432/dev pytest -q
