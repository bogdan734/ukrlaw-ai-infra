-- Job queue for long GPU jobs. Objects land in the current search_path so the
-- same script serves production and per-test schemas. Safe to re-run.

CREATE TABLE IF NOT EXISTS jobs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant       text NOT NULL,
    idem_key     text NOT NULL,
    workflow     text NOT NULL,
    pool         text NOT NULL,
    priority     int NOT NULL DEFAULT 100,  -- lower runs first
    params       jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    lease_token  uuid,
    lease_until  timestamptz,
    worker       text,
    progress     real,
    outputs      jsonb,
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz,
    UNIQUE (tenant, idem_key)
);

-- Matches claim_job's ORDER BY so claiming stays an index scan as history grows.
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (pool, priority, id) WHERE status = 'queued';

-- Keeps the reaper cheap: it only ever looks at running jobs.
CREATE INDEX IF NOT EXISTS jobs_lease_idx ON jobs (lease_until) WHERE status = 'running';

CREATE OR REPLACE FUNCTION enqueue(
    p_tenant text, p_idem_key text, p_workflow text, p_pool text,
    p_priority int DEFAULT 100, p_params jsonb DEFAULT '{}'
) RETURNS jobs LANGUAGE plpgsql AS $$
DECLARE
    r jobs;
BEGIN
    INSERT INTO jobs (tenant, idem_key, workflow, pool, priority, params)
    VALUES (p_tenant, p_idem_key, p_workflow, p_pool,
            coalesce(p_priority, 100), coalesce(p_params, '{}'))
    ON CONFLICT (tenant, idem_key) DO NOTHING
    RETURNING * INTO r;
    IF NOT FOUND THEN
        -- ON CONFLICT waited for any concurrent inserter to commit, so a fresh
        -- statement snapshot is guaranteed to see the winning row.
        SELECT * INTO r FROM jobs WHERE tenant = p_tenant AND idem_key = p_idem_key;
    END IF;
    RETURN r;
END
$$;

CREATE OR REPLACE FUNCTION claim_job(p_pool text, p_worker text, p_lease_s int)
RETURNS SETOF jobs LANGUAGE sql AS $$
    -- SKIP LOCKED lets concurrent workers pass over rows another worker is
    -- claiming instead of blocking on them or claiming them twice.
    WITH next AS (
        SELECT id FROM jobs
        WHERE pool = p_pool AND status = 'queued'
        ORDER BY priority, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE jobs j
    SET status      = 'running',
        attempts    = j.attempts + 1,
        lease_token = gen_random_uuid(),
        lease_until = now() + make_interval(secs => p_lease_s),
        worker      = p_worker,
        started_at  = coalesce(j.started_at, now())
    FROM next
    WHERE j.id = next.id
    RETURNING j.*;
$$;

CREATE OR REPLACE FUNCTION heartbeat(p_id bigint, p_token uuid, p_lease_s int, p_progress real)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    UPDATE jobs
    SET lease_until = now() + make_interval(secs => p_lease_s),
        -- A heartbeat without progress must not wipe the last reported value.
        progress    = coalesce(p_progress, progress)
    WHERE id = p_id AND status = 'running' AND lease_token = p_token;
    RETURN FOUND;
END
$$;

CREATE OR REPLACE FUNCTION complete_job(p_id bigint, p_token uuid, p_outputs jsonb)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    UPDATE jobs
    SET status = 'succeeded', outputs = p_outputs,
        lease_token = NULL, lease_until = NULL, finished_at = now()
    WHERE id = p_id AND status = 'running' AND lease_token = p_token;
    RETURN FOUND;
END
$$;

CREATE OR REPLACE FUNCTION fail_job(p_id bigint, p_token uuid, p_error text, p_retryable boolean)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    j jobs;
BEGIN
    -- Lock first so the retry decision and the update see the same attempts.
    SELECT * INTO j FROM jobs
    WHERE id = p_id AND status = 'running' AND lease_token = p_token
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF p_retryable AND j.attempts < j.max_attempts THEN
        UPDATE jobs
        SET status = 'queued', error = p_error, lease_token = NULL, lease_until = NULL
        WHERE id = p_id;
        RETURN 'requeued';
    END IF;
    UPDATE jobs
    SET status = 'failed', error = p_error,
        lease_token = NULL, lease_until = NULL, finished_at = now()
    WHERE id = p_id;
    RETURN 'failed';
END
$$;

CREATE OR REPLACE FUNCTION reap_expired()
RETURNS integer LANGUAGE sql AS $$
    -- Clearing the token is the fence: a slow worker that wakes up later can
    -- no longer heartbeat, complete or fail the job it lost.
    WITH reaped AS (
        UPDATE jobs
        SET status      = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
            error       = CASE WHEN attempts < max_attempts THEN error ELSE 'lease expired' END,
            finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
            lease_token = NULL,
            lease_until = NULL
        WHERE status = 'running' AND lease_until < now()
        RETURNING 1
    )
    SELECT count(*)::int FROM reaped;
$$;

CREATE OR REPLACE FUNCTION cancel_job(p_id bigint)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    -- Dropping the token makes a running worker's next heartbeat return false,
    -- which is its signal to stop burning GPU time.
    UPDATE jobs
    SET status = 'canceled', lease_token = NULL, lease_until = NULL, finished_at = now()
    WHERE id = p_id AND status IN ('queued', 'running');
    RETURN FOUND;
END
$$;
