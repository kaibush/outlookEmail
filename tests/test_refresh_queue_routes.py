import importlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('DATABASE_PATH', tempfile.mktemp(prefix='refresh-queue-tests-', suffix='.db'))
os.environ.setdefault('WERKZEUG_RUN_MAIN', 'false')
web = importlib.import_module('web_outlook_app')


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = str(tmp_path / 'accounts.db')
    monkeypatch.setattr(web, 'DATABASE', database)
    monkeypatch.setitem(web.app.config, 'TESTING', True)
    monkeypatch.setitem(web.app.config, 'WTF_CSRF_ENABLED', False)
    with web.app.app_context():
        web.init_db()
    monkeypatch.setattr(web, 'refresh_queue', web.RefreshQueue(database))
    client = web.app.test_client()
    with client.session_transaction() as session:
        session['logged_in'] = True
    return client


def add_account(status='active', last_success=None):
    with web.app.app_context():
        db = web.get_db()
        row = db.execute("INSERT INTO accounts(email,status,account_type,last_refresh_success_at) "
                         "VALUES (?,?,'outlook',?)", (f'{status}-{last_success}@example.com', status, last_success))
        db.commit()
        return row.lastrowid


def test_queue_endpoints_require_login(client):
    anonymous = web.app.test_client()
    assert anonymous.get('/api/accounts/refresh-jobs/latest').status_code in (302, 401)
    assert anonymous.post('/api/accounts/refresh-jobs', json={'mode': 'all'}).status_code in (302, 401)


def test_submit_returns_without_network_and_conflict_reattaches(client, monkeypatch):
    add_account()
    monkeypatch.setattr(web, 'refresh_outlook_account_token', lambda *a, **k: pytest.fail('submit must not refresh'))
    first = client.post('/api/accounts/refresh-jobs', json={'mode': 'all'})
    assert first.status_code == 202
    second = client.post('/api/accounts/refresh-jobs', json={'mode': 'all'})
    assert second.status_code == 409
    assert first.json['job']['id'] == second.json['job']['id']
    assert client.get('/api/accounts/refresh-jobs/latest').json['job']['total'] == 1


def test_due_selection_is_per_account(client):
    now = datetime.now(timezone.utc)
    new_id = add_account()
    old_id = add_account(last_success=(now - timedelta(days=40)).strftime('%Y-%m-%d %H:%M:%S'))
    add_account(last_success=now.strftime('%Y-%m-%d %H:%M:%S'))
    add_account(status='inactive')
    with web.app.app_context():
        web.enqueue_refresh_job('scheduled')
    with web.refresh_queue.connect() as db:
        ids = {row[0] for row in db.execute('SELECT account_id FROM refresh_job_items')}
    assert ids == {new_id, old_id}


def test_disabled_after_enqueue_is_skipped(client, monkeypatch):
    account_id = add_account()
    response = client.post('/api/accounts/refresh-jobs', json={'mode': 'selected', 'account_ids': [account_id]})
    assert response.status_code == 202
    with web.app.app_context():
        db = web.get_db()
        db.execute("UPDATE accounts SET status='inactive'")
        db.commit()
    monkeypatch.setattr(web, 'refresh_outlook_account_token', lambda *a, **k: pytest.fail('inactive account'))
    with web.refresh_queue.connect() as db:
        db.execute('UPDATE refresh_jobs SET delay_seconds=0')
    web.process_refresh_queue()
    assert client.get('/api/accounts/refresh-jobs/latest').json['job']['counts'] == {'skipped': 1}


@pytest.mark.parametrize('payload', [[], {'mode': []}, {'mode': 'other'}, {'mode': 'selected', 'account_ids': '1'}])
def test_invalid_payload(client, payload):
    assert client.post('/api/accounts/refresh-jobs', json=payload).status_code == 400
