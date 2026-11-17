"""SQLite-backed, single-consumer refresh queue shared by application workers.

Only account IDs are queued. Credentials are loaded immediately before use.
A renewable lease permits recovery after process termination; completed items
and rotated credentials are committed together. An in-flight request may be
repeated after a crash (Microsoft requests cannot participate in a DB transaction).
"""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


class RefreshQueue:
    LEASE_SECONDS = 120

    def __init__(self, database):
        self.database = database
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS refresh_jobs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    started_at REAL, finished_at REAL, owner TEXT, lease_until REAL,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    delay_seconds INTEGER NOT NULL DEFAULT 5
                );
                CREATE UNIQUE INDEX IF NOT EXISTS refresh_jobs_one_active
                    ON refresh_jobs((1)) WHERE status IN ('queued', 'running');
                CREATE TABLE IF NOT EXISTS refresh_job_items (
                    job_id TEXT NOT NULL, account_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', result TEXT,
                    finished_at REAL,
                    PRIMARY KEY (job_id, account_id)
                );
                CREATE INDEX IF NOT EXISTS refresh_job_items_status
                    ON refresh_job_items(job_id, status, account_id);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, mode, account_ids, delay_seconds=5):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT id FROM refresh_jobs WHERE status IN ('queued','running')").fetchone()
            if active:
                return active['id'], False
            job_id = uuid.uuid4().hex
            db.execute('INSERT INTO refresh_jobs(id,mode,created_at,updated_at,delay_seconds) VALUES(?,?,?,?,?)',
                       (job_id, mode, now, now, max(0, min(60, int(delay_seconds)))))
            db.executemany('INSERT OR IGNORE INTO refresh_job_items(job_id,account_id) VALUES(?,?)',
                           ((job_id, account_id) for account_id in account_ids))
            return job_id, True

    def snapshot(self, job_id=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM refresh_jobs WHERE id=?' if job_id else
                             'SELECT * FROM refresh_jobs ORDER BY created_at DESC LIMIT 1',
                             (job_id,) if job_id else ()).fetchone()
            if row is None:
                return None
            job = dict(row)
            job.pop('owner', None)
            counts = {r['status']: r['n'] for r in db.execute(
                'SELECT status,COUNT(*) n FROM refresh_job_items WHERE job_id=? GROUP BY status', (job['id'],))}
            job['counts'] = counts
            job['total'] = sum(counts.values())
            job['completed'] = sum(counts.get(s, 0) for s in ('success', 'failed', 'skipped'))
            job['remaining'] = counts.get('pending', 0) + counts.get('running', 0)
            job['recovering'] = job['status'] == 'running' and (job['lease_until'] or 0) < time.time()
            job['results'] = [dict(r) | {'result': json.loads(r['result'] or '{}')} for r in db.execute(
                'SELECT account_id,status,result,finished_at FROM refresh_job_items '
                'WHERE job_id=? AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 30', (job['id'],))]
            return job

    def stop(self, job_id):
        with self.connect() as db:
            return db.execute("UPDATE refresh_jobs SET stop_requested=1,updated_at=? "
                              "WHERE id=? AND status IN ('queued','running')", (time.time(), job_id)).rowcount > 0

    def resume(self, job_id):
        with self.connect() as db:
            job = db.execute("SELECT mode,delay_seconds FROM refresh_jobs WHERE id=? AND status='stopped'",
                             (job_id,)).fetchone()
            if job is None:
                raise ValueError('只能继续已停止的任务')
            ids = [r[0] for r in db.execute("SELECT account_id FROM refresh_job_items WHERE job_id=? AND status='pending'",
                                            (job_id,))]
        if not ids:
            raise ValueError('没有未完成的账号')
        return self.submit(job['mode'], ids, job['delay_seconds'])

    def claim(self):
        now = time.time()
        owner = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM refresh_jobs WHERE status='queued' OR "
                             "(status='running' AND lease_until<?) ORDER BY created_at LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            job = dict(row)
            db.execute("UPDATE refresh_jobs SET status='running',owner=?,lease_until=?,"
                       "started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                       (owner, now + self.LEASE_SECONDS, now, now, job['id']))
            db.execute("UPDATE refresh_job_items SET status='pending' WHERE job_id=? AND status='running'", (job['id'],))
            job['owner'] = owner
            return job

    def run_next(self, process):
        job = self.claim()
        if job is None:
            return
        done = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not done.wait(self.LEASE_SECONDS / 4):
                try:
                    with self.connect() as db:
                        now = time.time()
                        updated = db.execute('UPDATE refresh_jobs SET lease_until=?,updated_at=? '
                                             "WHERE id=? AND owner=? AND status='running'",
                                             (now + self.LEASE_SECONDS, now, job['id'], job['owner'])).rowcount
                        if not updated:
                            lost.set()
                            return
                except sqlite3.Error:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True, name='token-refresh-heartbeat')
        thread.start()
        try:
            while not lost.is_set():
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT * FROM refresh_jobs WHERE id=?', (job['id'],)).fetchone()
                    if current['owner'] != job['owner']:
                        return
                    item = db.execute("SELECT account_id FROM refresh_job_items WHERE job_id=? AND status='pending' "
                                      'ORDER BY account_id LIMIT 1', (job['id'],)).fetchone()
                    if current['stop_requested'] or item is None:
                        status = 'stopped' if current['stop_requested'] else 'completed'
                        db.execute('UPDATE refresh_jobs SET status=?,finished_at=?,updated_at=?,lease_until=NULL '
                                   'WHERE id=?', (status, time.time(), time.time(), job['id']))
                        return
                    account_id = item['account_id']
                    db.execute("UPDATE refresh_job_items SET status='running' WHERE job_id=? AND account_id=?",
                               (job['id'], account_id))
                # The callback writes credentials and refresh logs into this transaction.
                with self.connect() as db:
                    result = process(account_id, job['mode'], db)
                    # Fence a recovered worker before committing any account mutations.
                    if not db.in_transaction:
                        db.execute('BEGIN IMMEDIATE')
                    owner = db.execute('SELECT owner FROM refresh_jobs WHERE id=?', (job['id'],)).fetchone()['owner']
                    if owner != job['owner'] or lost.is_set():
                        db.rollback()
                        return
                    status = 'skipped' if result.get('skipped') else ('success' if result.get('success') else 'failed')
                    db.execute('UPDATE refresh_job_items SET status=?,result=?,finished_at=? WHERE job_id=? AND account_id=?',
                               (status, json.dumps(result, ensure_ascii=False), time.time(), job['id'], account_id))
                    db.execute('UPDATE refresh_jobs SET updated_at=? WHERE id=?', (time.time(), job['id']))
                deadline = time.monotonic() + job['delay_seconds']
                while time.monotonic() < deadline and not lost.is_set():
                    with self.connect() as db:
                        if db.execute('SELECT stop_requested FROM refresh_jobs WHERE id=?', (job['id'],)).fetchone()[0]:
                            break
                    done.wait(min(1, max(0, deadline - time.monotonic())))
        finally:
            done.set()
            thread.join(timeout=2)
            # Unexpected interruption leaves only unfinished work for the next worker.
            with self.connect() as db:
                db.execute("UPDATE refresh_jobs SET lease_until=0 WHERE id=? AND owner=? AND status='running'",
                           (job['id'], job['owner']))
