import importlib
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
_temp_dir = tempfile.mkdtemp(prefix='outlookEmail-batch-status-')
os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

web_outlook_app = importlib.import_module('web_outlook_app')
ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]


class BatchAccountStatusApiTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        with self.client.session_transaction() as sess:
            sess['logged_in'] = True

        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM account_tags')
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM account_refresh_logs')
            db.execute('DELETE FROM accounts')
            db.commit()

            self.assertTrue(web_outlook_app.add_account(
                'active-one@outlook.com',
                'password123',
                'client-active',
                'refresh-active',
                group_id=1,
                status='active',
            ))
            self.assertTrue(web_outlook_app.add_account(
                'inactive-one@outlook.com',
                'password123',
                'client-inactive',
                'refresh-inactive',
                group_id=1,
                status='inactive',
            ))
            active_account = web_outlook_app.get_account_by_email('active-one@outlook.com')
            inactive_account = web_outlook_app.get_account_by_email('inactive-one@outlook.com')
            self.active_account_id = active_account['id']
            self.inactive_account_id = inactive_account['id']

    def test_batch_disable_updates_only_active_accounts_in_one_request(self):
        response = self.client.post(
            '/api/accounts/batch-update-status',
            json={
                'account_ids': [self.active_account_id, self.inactive_account_id, 999999],
                'status': 'inactive',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['status'], 'inactive')
        self.assertEqual(payload['updated_count'], 1)
        self.assertEqual(payload['unchanged_count'], 1)
        self.assertEqual(payload['missing_ids'], [999999])
        self.assertEqual(payload['updated_accounts'][0]['email'], 'active-one@outlook.com')

        with self.app.app_context():
            active_account = web_outlook_app.get_account_by_id(self.active_account_id)
            inactive_account = web_outlook_app.get_account_by_id(self.inactive_account_id)

        self.assertEqual(active_account['status'], 'inactive')
        self.assertEqual(inactive_account['status'], 'inactive')

    def test_batch_enable_updates_only_inactive_accounts(self):
        response = self.client.post(
            '/api/accounts/batch-update-status',
            json={
                'account_ids': [self.active_account_id, self.inactive_account_id],
                'status': 'active',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['updated_count'], 1)
        self.assertEqual(payload['unchanged_count'], 1)
        self.assertEqual(payload['updated_accounts'][0]['id'], self.inactive_account_id)

        with self.app.app_context():
            active_account = web_outlook_app.get_account_by_id(self.active_account_id)
            inactive_account = web_outlook_app.get_account_by_id(self.inactive_account_id)

        self.assertEqual(active_account['status'], 'active')
        self.assertEqual(inactive_account['status'], 'active')

    def test_batch_status_rejects_invalid_status_without_changing_accounts(self):
        response = self.client.post(
            '/api/accounts/batch-update-status',
            json={
                'account_ids': [self.active_account_id],
                'status': 'disabled',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('active', payload['error'])

        with self.app.app_context():
            active_account = web_outlook_app.get_account_by_id(self.active_account_id)
        self.assertEqual(active_account['status'], 'active')

    def test_batch_status_chunks_updates_under_sqlite_variable_limit(self):
        with self.app.app_context():
            parsed_accounts = [
                {
                    'email': f'bulk-{index}@outlook.com',
                    'password': 'password123',
                    'client_id': 'client',
                    'refresh_token': 'token',
                }
                for index in range(12)
            ]
            result = web_outlook_app.add_accounts_bulk(parsed_accounts, group_id=1, status='active')
            self.assertEqual(result['added_count'], 12)
            rows = web_outlook_app.get_db().execute(
                "SELECT id FROM accounts WHERE email LIKE 'bulk-%@outlook.com' ORDER BY id"
            ).fetchall()
            account_ids = [int(row['id']) for row in rows]
            db = web_outlook_app.get_db()
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
            original_chunk = web_outlook_app.chunk_account_ids

            def chunk_by_three(ids, chunk_size=500):
                return original_chunk(ids, 3)

            with patch.object(web_outlook_app, 'chunk_account_ids', side_effect=chunk_by_three):
                payload = web_outlook_app.update_accounts_status_by_ids(account_ids, 'inactive')

            self.assertTrue(payload['success'])
            self.assertEqual(payload['updated_count'], 12)
            statuses = web_outlook_app.get_db().execute(
                "SELECT status FROM accounts WHERE email LIKE 'bulk-%@outlook.com'"
            ).fetchall()
            self.assertTrue(all(row['status'] == 'inactive' for row in statuses))


class BatchAccountStatusFrontendTests(unittest.TestCase):
    def test_batch_menus_send_one_status_request(self):
        layout_html = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'layout.html').read_text(encoding='utf-8')
        refresh_html = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'dialogs-management.html').read_text(encoding='utf-8')
        batch_js = (ROOT_DIR / 'static' / 'js' / 'index' / '10-batch-actions.js').read_text(encoding='utf-8')
        refresh_js = (ROOT_DIR / 'static' / 'js' / 'index' / '08-refresh.js').read_text(encoding='utf-8')
        groups_js = (ROOT_DIR / 'static' / 'js' / 'index' / '02-groups.js').read_text(encoding='utf-8')

        self.assertIn('id="batchEnableAccountsBtn"', layout_html)
        self.assertIn('id="batchDisableAccountsBtn"', layout_html)
        self.assertIn('onclick="enableSelectedAccounts()"', layout_html)
        self.assertIn('onclick="disableSelectedAccounts()"', layout_html)
        self.assertIn('id="refreshEnableAccountsBtn"', refresh_html)
        self.assertIn('id="refreshDisableAccountsBtn"', refresh_html)
        self.assertIn('data-account-status=', groups_js)
        self.assertIn("fetch('/api/accounts/batch-update-status'", batch_js)
        self.assertIn('account_ids: accountIds', batch_js)
        self.assertNotIn('/api/accounts/${accountId}', batch_js.split('async function updateStatusForSelectedAccounts', 1)[1].split('async function enableSelectedAccounts', 1)[0])
        self.assertIn("updateStatusForSelectedAccounts('inactive')", refresh_js)
        self.assertIn('isTempContext ? \'none\' : \'inline-flex\'', batch_js)


if __name__ == '__main__':
    unittest.main()
