from unittest.mock import Mock

import pytest

from tests.test_refresh_queue_routes import client, web, add_account


@pytest.fixture
def account(client):
    account_id = add_account()
    with web.app.app_context():
        db = web.get_db()
        db.execute("UPDATE accounts SET password=?,client_id='old-client',refresh_token=?,"
                   "authorization_type='imap',remark='keep',last_refresh_status='failed' WHERE id=?",
                   (web.encrypt_data('test-password'), web.encrypt_data('old-token'), account_id))
        db.commit()
    return account_id


def run_job(client, account, monkeypatch, *, channel='auto', extracted=None, validated=None):
    extract = Mock(return_value=extracted or {'success': True, 'client_id': 'new-client', 'refresh_token': 'new-token'})
    response = Mock(status_code=200)
    response.json.return_value = validated if validated is not None else {'access_token': 'access', 'refresh_token': 'rotated-token'}
    validate = Mock(return_value=response)
    monkeypatch.setattr(web, 'extract_graph_refresh_token', extract)
    monkeypatch.setattr(web, 'request_imap_token_response', validate)
    monkeypatch.setattr(web, 'request_graph_token_response', validate)
    result = client.post('/api/accounts/reauthorization-jobs', json={
        'scope': 'selected', 'channel': channel, 'account_ids': [account]})
    assert result.status_code == 202
    assert not extract.called
    with web.refresh_queue.connect() as db:
        db.execute('UPDATE refresh_jobs SET delay_seconds=0')
    web.process_refresh_queue()
    return extract, validate, client.get('/api/accounts/refresh-jobs/latest').json['job']


def current(account):
    with web.app.app_context():
        return dict(web.get_db().execute('SELECT * FROM accounts WHERE id=?', (account,)).fetchone())


@pytest.mark.parametrize('channel,expected', [('auto', 'imap'), ('graph', 'graph'), ('imap', 'imap')])
def test_success_replaces_only_authorization_after_validation(client, account, monkeypatch, channel, expected):
    extract, validate, job = run_job(client, account, monkeypatch, channel=channel)
    row = current(account)
    assert web.decrypt_data(row['refresh_token']) == 'rotated-token'
    assert row['client_id'] == 'new-client'
    assert row['authorization_type'] == expected
    assert row['remark'] == 'keep'
    assert web.decrypt_data(row['password']) == 'test-password'
    assert row['last_refresh_status'] == 'success'
    assert row['last_refresh_success_at']
    assert row['refresh_token_updated_at']
    assert extract.call_args.kwargs['scope'] == web.GRAPH_EXTRACT_SCOPE_BY_MODE[expected]
    assert validate.called
    assert job['counts'] == {'success': 1}
    assert not any(secret in str(job) for secret in ('test-password', 'new-token', 'rotated-token', 'old-token'))


def test_interaction_failure_preserves_old_authorization(client, account, monkeypatch):
    extract, validate, job = run_job(client, account, monkeypatch, extracted={
        'success': False, 'error': '需要额外验证', 'details': 'MFA test-password'})
    assert not validate.called
    row = current(account)
    assert row['client_id'] == 'old-client'
    assert web.decrypt_data(row['refresh_token']) == 'old-token'
    assert job['results'][0]['result']['needs_interaction']
    assert 'test-password' not in str(job)
    assert 'test-password' not in row['last_refresh_error']


def test_failed_validation_does_not_save_new_token(client, account, monkeypatch):
    _, _, job = run_job(client, account, monkeypatch, validated={'error': 'invalid_grant'})
    assert job['counts'] == {'failed': 1}
    assert web.decrypt_data(current(account)['refresh_token']) == 'old-token'


def test_missing_password_does_not_attempt_login(client, account, monkeypatch):
    with web.refresh_queue.connect() as db:
        db.execute("UPDATE accounts SET password='' WHERE id=?", (account,))
    extract, _, job = run_job(client, account, monkeypatch)
    assert not extract.called
    assert job['results'][0]['result']['needs_interaction']


def test_account_edit_during_authorization_is_not_overwritten(client, account, monkeypatch):
    def extract(*args, **kwargs):
        with web.refresh_queue.connect() as db:
            db.execute('UPDATE accounts SET refresh_token=? WHERE id=?', (web.encrypt_data('manually-updated'), account))
        return {'success': True, 'client_id': 'new-client', 'refresh_token': 'new-token'}
    monkeypatch.setattr(web, 'extract_graph_refresh_token', extract)
    response = Mock(status_code=200)
    response.json.return_value = {'access_token': 'access'}
    monkeypatch.setattr(web, 'request_imap_token_response', lambda *a, **kw: response)
    with web.app.app_context(), web.refresh_queue.connect() as db:
        row = db.execute('SELECT * FROM accounts WHERE id=?', (account,)).fetchone()
        result = web.process_queued_refresh_account(account, 'reauthorize:auto', db)
    assert result['skipped']
    assert web.decrypt_data(current(account)['refresh_token']) == 'manually-updated'


def test_failed_scope_only_enqueues_active_failed_accounts(client, account):
    other = add_account('inactive')
    with web.refresh_queue.connect() as db:
        db.execute("UPDATE accounts SET last_refresh_status='failed' WHERE id=?", (other,))
    response = client.post('/api/accounts/reauthorization-jobs', json={'scope': 'failed', 'channel': 'auto'})
    assert response.status_code == 202
    with web.refresh_queue.connect() as db:
        assert [r[0] for r in db.execute('SELECT account_id FROM refresh_job_items')] == [account]
    assert client.post('/api/accounts/refresh-jobs', json={'mode': 'all'}).status_code == 409


@pytest.mark.parametrize('data', [[], {'scope': []}, {'channel': {}}, {'scope': 'all'}, {'account_ids': '1'}])
def test_invalid_requests(client, data):
    assert client.post('/api/accounts/reauthorization-jobs', json=data).status_code == 400


def test_authorization_job_requires_login(client):
    response = web.app.test_client().post('/api/accounts/reauthorization-jobs', json={'scope': 'failed'})
    assert response.status_code in (302, 401)


def test_browser_engine_dispatch_and_repair_only_on_expiry(client, account, monkeypatch):
    from outlook_web import browser_oauth
    browser = Mock(return_value={'success': False, 'error': 'MFA required'})
    monkeypatch.setattr(browser_oauth, 'browser_refresh_token', browser)
    monkeypatch.setattr(web, 'refresh_outlook_account_token', Mock(return_value={
        'success': False, 'error_message': 'invalid_grant: expired'}))
    response = client.post('/api/accounts/reauthorization-jobs', json={
        'scope':'selected','account_ids':[account],'engine':'browser','action':'repair'})
    assert response.status_code == 202
    assert response.json['job']['mode'] == 'repair:browser:auto'
    with web.app.app_context(), web.refresh_queue.connect() as db:
        result = web.process_queued_refresh_account(account, 'repair:browser:auto', db)
    assert browser.call_count == 1
    assert not result['success']
    browser.reset_mock()
    web.refresh_outlook_account_token.return_value = {'success': False, 'error_message': 'Connection timed out'}
    with web.app.app_context(), web.refresh_queue.connect() as db:
        web.process_queued_refresh_account(account, 'repair:browser:auto', db)
    assert not browser.called


@pytest.mark.parametrize('http_result, browser_expected', [
    ({'success':True,'client_id':'client','refresh_token':'fresh'}, False),
    ({'success':False,'error':'无法提取 Flow Token'}, True),
    ({'success':False,'error':'需要额外验证'}, True),
    ({'success':False,'error':'Connection timed out'}, False),
    ({'success':False,'error':'密码不正确或账号不存在'}, False),
])
def test_http_first_only_uses_browser_for_interactive_failures(client, account, monkeypatch, http_result, browser_expected):
    from outlook_web import browser_oauth
    http = Mock(return_value=http_result)
    browser = Mock(return_value={'success':True,'client_id':'client','refresh_token':'browser-fresh'})
    monkeypatch.setattr(web,'extract_graph_refresh_token',http)
    monkeypatch.setattr(browser_oauth,'browser_refresh_token',browser)
    response=Mock(status_code=200)
    response.json.return_value={'access_token':'valid'}
    monkeypatch.setattr(web,'request_imap_token_response',Mock(return_value=response))
    with web.app.app_context(), web.refresh_queue.connect() as db:
        result=web.process_queued_refresh_account(account,'reauthorize:fallback:auto',db)
    assert http.call_count == 1
    assert browser.called == browser_expected
    assert result['success'] == (http_result['success'] or browser_expected)
