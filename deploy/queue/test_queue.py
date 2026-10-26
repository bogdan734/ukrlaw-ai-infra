import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


@pytest.fixture(scope="session")
def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    name = f"queue_test_{uuid.uuid4().hex[:12]}"

    def _connect():
        return psycopg.connect(url, autocommit=True, row_factory=dict_row,
                               options=f"-c search_path={name}")

    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            with _connect() as conn:
                # Twice, because deploys re-run the script against a live schema.
                conn.execute(SCHEMA_SQL)
                conn.execute(SCHEMA_SQL)
            yield _connect
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def db(connect):
    with connect() as conn:
        conn.execute("TRUNCATE jobs RESTART IDENTITY")
        yield conn


def scalar(conn, query, *args):
    return next(iter(conn.execute(query, args).fetchone().values()))


def enqueue(conn, key, tenant="t1", pool="gpu", priority=100):
    return conn.execute("SELECT * FROM enqueue(%s, %s, %s, %s, %s, %s)",
                        (tenant, key, "render", pool, priority, Jsonb({"key": key}))).fetchone()


def claim(conn, pool="gpu", worker="w1", lease_s=60):
    return conn.execute("SELECT * FROM claim_job(%s, %s, %s)", (pool, worker, lease_s)).fetchone()


def heartbeat(conn, job_id, token, lease_s=60, progress=None):
    return scalar(conn, "SELECT heartbeat(%s, %s, %s, %s::real)", job_id, token, lease_s, progress)


def complete(conn, job_id, token, outputs=None):
    return scalar(conn, "SELECT complete_job(%s, %s, %s)", job_id, token, Jsonb(outputs or {}))


def fail(conn, job_id, token, retryable=True):
    return scalar(conn, "SELECT fail_job(%s, %s, %s, %s)", job_id, token, "boom", retryable)


def expire(conn, job_id):
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))


def reap(conn):
    return scalar(conn, "SELECT reap_expired()")


def get(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def test_enqueue_is_idempotent_per_tenant_key(db):
    first = enqueue(db, "k1", priority=5)
    again = enqueue(db, "k1", priority=50)
    other_tenant = enqueue(db, "k1", tenant="t2")
    assert again == first
    assert other_tenant["id"] != first["id"]
    assert scalar(db, "SELECT count(*) FROM jobs") == 2


def test_claim_respects_pool_and_priority_order(db):
    low = enqueue(db, "a", priority=200)["id"]
    high1 = enqueue(db, "b", priority=10)["id"]
    high2 = enqueue(db, "c", priority=10)["id"]
    cpu = enqueue(db, "d", pool="cpu", priority=1)["id"]
    first = claim(db, worker="gpu-0")
    assert first["status"] == "running" and first["attempts"] == 1
    assert first["worker"] == "gpu-0" and first["lease_token"] and first["started_at"]
    assert [first["id"], claim(db)["id"], claim(db)["id"]] == [high1, high2, low]
    assert claim(db) is None
    assert claim(db, pool="cpu")["id"] == cpu


def test_claim_returns_nothing_when_queue_is_empty(db):
    assert claim(db) is None
    enqueue(db, "only")
    assert claim(db) is not None
    assert claim(db) is None


def test_concurrent_workers_never_claim_the_same_job(db, connect):
    ids = sorted(enqueue(db, f"job-{i}")["id"] for i in range(60))
    barrier = threading.Barrier(8)

    def worker(n):
        claimed = []
        with connect() as conn:
            barrier.wait()
            while (job := claim(conn, worker=f"w{n}")) is not None:
                claimed.append(job["id"])
        return claimed

    with ThreadPoolExecutor(8) as pool:
        results = [f.result() for f in [pool.submit(worker, n) for n in range(8)]]
    assert sorted(i for claimed in results for i in claimed) == ids
    assert scalar(db, "SELECT count(*) FROM jobs WHERE status = 'running' AND attempts = 1") == 60


def test_heartbeat_extends_lease_and_rejects_wrong_token(db):
    enqueue(db, "hb")
    job = claim(db, lease_s=5)
    assert heartbeat(db, job["id"], job["lease_token"], lease_s=600, progress=0.5)
    row = get(db, job["id"])
    assert row["lease_until"] > job["lease_until"]
    assert row["progress"] == 0.5
    assert not heartbeat(db, job["id"], uuid.uuid4(), lease_s=600)
    assert get(db, job["id"])["lease_until"] == row["lease_until"]


def test_reaper_requeues_expired_lease_and_clears_token(db):
    enqueue(db, "r")
    job = claim(db)
    expire(db, job["id"])
    assert reap(db) == 1
    row = get(db, job["id"])
    assert row["status"] == "queued" and row["attempts"] == 1
    assert row["lease_token"] is None and row["lease_until"] is None
    assert not heartbeat(db, job["id"], job["lease_token"])
    assert reap(db) == 0


def test_stale_worker_cannot_complete_after_requeue(db, connect):
    enqueue(db, "stale")
    with connect() as conn_b:
        job_a = claim(db, worker="A")
        expire(db, job_a["id"])
        assert reap(db) == 1
        job_b = claim(conn_b, worker="B")
        assert job_b["id"] == job_a["id"] and job_b["attempts"] == 2
        assert not complete(db, job_a["id"], job_a["lease_token"], {"by": "A"})
        assert complete(conn_b, job_b["id"], job_b["lease_token"], {"by": "B"})
    row = get(db, job_a["id"])
    assert row["status"] == "succeeded" and row["outputs"] == {"by": "B"}
    assert row["finished_at"] is not None and row["lease_token"] is None


def test_retry_budget_exhausts_to_failed(db):
    job_id = enqueue(db, "flaky")["id"]
    for expected in ["requeued", "requeued", "failed"]:
        job = claim(db)
        assert fail(db, job_id, job["lease_token"]) == expected
    row = get(db, job_id)
    assert row["status"] == "failed" and row["attempts"] == 3 and row["error"] == "boom"
    assert row["finished_at"] is not None and claim(db) is None
    assert fail(db, job_id, job["lease_token"]) is None

    lost = enqueue(db, "lost")["id"]
    db.execute("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (lost,))
    claim(db)
    expire(db, lost)
    assert reap(db) == 1
    row = get(db, lost)
    assert row["status"] == "failed" and row["error"] == "lease expired"


def test_cancel_running_job_revokes_lease(db):
    enqueue(db, "cancel-me")
    job = claim(db)
    assert scalar(db, "SELECT cancel_job(%s)", job["id"])
    row = get(db, job["id"])
    assert row["status"] == "canceled" and row["lease_token"] is None
    assert not heartbeat(db, job["id"], job["lease_token"])
    assert not complete(db, job["id"], job["lease_token"])
    assert not scalar(db, "SELECT cancel_job(%s)", job["id"])
    queued = enqueue(db, "never-runs")["id"]
    assert scalar(db, "SELECT cancel_job(%s)", queued)
    assert claim(db) is None
