import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from outlook_web.token_refresh_queue import RefreshQueue


@pytest.fixture
def queue(tmp_path):
    return RefreshQueue(str(tmp_path / 'queue.db'))


def test_submission_is_deduplicated_across_workers(queue):
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: queue.submit('all', range(14000), 0), range(4)))
    assert sum(created for _, created in results) == 1
    assert len({job_id for job_id, _ in results}) == 1
    assert queue.snapshot()['total'] == 14000


def test_only_one_consumer_can_claim_and_expired_lease_is_recoverable(queue):
    queue.submit('all', [1, 2], 0)
    first = queue.claim()
    assert queue.claim() is None
    with queue.connect() as db:
        db.execute("UPDATE refresh_job_items SET status='success' WHERE account_id=1")
        db.execute("UPDATE refresh_job_items SET status='running' WHERE account_id=2")
        db.execute('UPDATE refresh_jobs SET lease_until=0')
    calls = []
    queue.run_next(lambda account_id, mode, db: calls.append(account_id) or {'success': True})
    assert calls == [2]
    assert queue.snapshot()['counts'] == {'success': 2}
    assert queue.snapshot()['status'] == 'completed'
    assert first['owner']


def test_stop_preserves_unprocessed_accounts(queue):
    job_id, _ = queue.submit('all', [1, 2, 3], 0)

    def process(account_id, mode, db):
        queue.stop(job_id)
        return {'success': True}

    queue.run_next(process)
    assert queue.snapshot()['status'] == 'stopped'
    assert queue.snapshot()['completed'] == 1
    assert queue.snapshot()['remaining'] == 2
    assert queue.submit('failed', [4], 0)[1]


def test_stop_queued_job_makes_no_outbound_requests(queue):
    job_id, _ = queue.submit('all', [1], 0)
    queue.stop(job_id)
    queue.run_next(lambda *args: pytest.fail('stopped job must not refresh'))
    assert queue.snapshot()['status'] == 'stopped'


def test_account_updates_and_completion_rollback_together(queue):
    queue.submit('all', [1], 0)
    with queue.connect() as db:
        db.execute('CREATE TABLE test_tokens(value TEXT)')

    def crash(account_id, mode, db):
        db.execute("INSERT INTO test_tokens VALUES ('rotated')")
        raise RuntimeError('process interrupted')

    with pytest.raises(RuntimeError):
        queue.run_next(crash)
    with queue.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM test_tokens').fetchone()[0] == 0
    assert queue.snapshot()['completed'] == 0
    queue.run_next(lambda *args: {'success': True, 'token_rotated': True})
    assert queue.snapshot()['completed'] == 1


def test_lost_lease_cannot_commit_result(queue):
    queue.submit('all', [1], 0)

    def lost_owner(account_id, mode, db):
        with queue.connect() as other:
            other.execute("UPDATE refresh_jobs SET owner='replacement'")
        return {'success': True}

    queue.run_next(lost_owner)
    assert queue.snapshot()['completed'] == 0


def test_failures_and_skips_are_counted_without_stopping_job(queue):
    queue.submit('selected', [1, 2, 3], 0)
    results = {1: {'success': False, 'error_message': 'revoked'},
               2: {'skipped': True}, 3: {'success': True, 'token_rotated': False}}
    queue.run_next(lambda account_id, mode, db: results[account_id])
    job = queue.snapshot()
    assert job['status'] == 'completed'
    assert job['counts'] == {'success': 1, 'failed': 1, 'skipped': 1}
    assert job['remaining'] == 0
    assert 'owner' not in job
    assert len(job['results']) == 3


def test_resume_only_queues_unfinished_accounts(queue):
    job_id, _ = queue.submit('all', [1, 2, 3], 0)

    def first(account_id, mode, db):
        queue.stop(job_id)
        return {'success': True}

    queue.run_next(first)
    resumed, created = queue.resume(job_id)
    assert created and resumed != job_id
    assert queue.snapshot()['total'] == 2
    calls = []
    queue.run_next(lambda account_id, mode, db: calls.append(account_id) or {'success': True})
    assert calls == [2, 3]
