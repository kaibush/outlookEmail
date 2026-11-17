import os
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, urlencode
from unittest.mock import Mock

import pytest

from outlook_web.browser_oauth import browser_refresh_token, proxy_options


def test_proxy_credentials_and_unsupported_socks():
    assert proxy_options('http://u:p%40ss@localhost:8080') == {
        'server': 'http://localhost:8080', 'username': 'u', 'password': 'p@ss'}
    assert proxy_options('direct') is None
    with pytest.raises(ValueError, match='SOCKS5'):
        proxy_options('socks5://u:p@localhost:1080')


@pytest.mark.parametrize('scenario', ['success', 'mfa', 'wrong_state', 'denied'])
def test_real_browser_login_and_callback_without_external_network(monkeypatch, scenario):
    sync = pytest.importorskip('playwright.sync_api')
    executable = '/usr/bin/google-chrome'
    if not os.path.exists(executable):
        pytest.skip('local Chromium is required for this browser integration test')
    monkeypatch.setenv('OAUTH_BROWSER_EXECUTABLE', executable)
    seen = []
    browsers = []

    @contextmanager
    def factory():
        with sync.sync_playwright() as pw:
            def launch(**kwargs):
                browser = pw.chromium.launch(**kwargs)
                browsers.append(browser)
                def new_context():
                    context = browser.new_context()
                    def routes(pattern, callback):
                        def handler(route):
                            url = urlsplit(route.request.url)
                            if url.hostname == 'localhost':
                                return callback(route)
                            seen.append((url.path, route.request.post_data or ''))
                            params = parse_qs(url.query)
                            if url.path.endswith('/authorize'):
                                state = params['state'][0]
                                assert params.get('code_challenge_method') == ['S256']
                                html = f'<form method="POST" action="/password?state={state}"><input name="loginfmt"><input id="idSIButton9" type="submit"></form>'
                            elif url.path == '/password':
                                state = params['state'][0]
                                html = f'<form method="POST" action="/finish?state={state}"><input name="passwd" type="password"><input id="idSIButton9" type="submit"></form>'
                            else:
                                if scenario == 'mfa':
                                    return route.fulfill(body='<input name="otc" autocomplete="one-time-code">',content_type='text/html')
                                state = 'wrong' if scenario == 'wrong_state' else params['state'][0]
                                target = 'http://localhost:8080/?' + urlencode({'state':state, 'error' if scenario == 'denied' else 'code':'fixture-code'})
                                return route.fulfill(status=302,headers={'Location':target})
                            route.fulfill(body=html,content_type='text/html')
                        context.route(pattern, handler)
                    return SimpleNamespace(route=routes, new_page=context.new_page)
                return SimpleNamespace(new_context=new_context, close=browser.close)
            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    response = Mock(status_code=200)
    response.json.return_value = {'refresh_token':'fixture-refresh-token'}
    exchange = Mock(return_value=response)
    result = browser_refresh_token('test@example.com','fixture-password',client_id='fixture-client',
                                   redirect_uri='http://localhost:8080/',scope='offline_access',
                                   playwright_factory=factory,exchange=exchange,timeout_seconds=10)
    assert not browsers[0].is_connected()
    assert any('loginfmt=test%40example.com' in payload for _,payload in seen)
    assert any('passwd=fixture-password' in payload for _,payload in seen)
    if scenario == 'success':
        assert result.get('success'), result
        assert result['refresh_token'] == 'fixture-refresh-token'
        assert exchange.call_args.kwargs['data']['code_verifier']
        assert exchange.call_args.kwargs['data']['code'] == 'fixture-code'
    else:
        assert not result['success']
        assert not exchange.called
        assert 'fixture-password' not in str(result)
