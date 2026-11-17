"""Isolated Chromium OAuth login. No cookies, passwords or screenshots persist."""
import base64
import hashlib
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit, parse_qs, unquote

import requests

LOGIN_HOSTS = {'login.live.com', 'login.microsoftonline.com', 'account.live.com'}


def proxy_options(value):
    if not value or value == 'direct':
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https', 'socks5'} or not parsed.hostname:
        raise ValueError('浏览器授权代理格式不支持')
    if parsed.scheme == 'socks5' and parsed.username:
        raise ValueError('Chromium 不支持带用户名密码的 SOCKS5 代理，请为账号配置 HTTP 代理')
    host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    proxy = {'server': f'{parsed.scheme}://{host}' + (f':{parsed.port}' if parsed.port else '')}
    if parsed.username:
        proxy.update(username=unquote(parsed.username), password=unquote(parsed.password or ''))
    return proxy


def browser_refresh_token(email, password, *, client_id, redirect_uri, scope,
                          proxy_url='', timeout_seconds=120, playwright_factory=None,
                          exchange=None):
    """Automate ordinary login/consent; stop at MFA/CAPTCHA rather than bypass it."""
    from playwright.sync_api import sync_playwright
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    callback = urlsplit(redirect_uri)
    captured = {}
    authorize_url = 'https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?' + urlencode({
        'client_id': client_id, 'redirect_uri': redirect_uri, 'response_type': 'code',
        'response_mode': 'query', 'scope': scope, 'state': state, 'login_hint': email,
        'prompt': 'login', 'code_challenge': challenge, 'code_challenge_method': 'S256',
    })

    def fail(message):
        return {'success': False, 'error': message, 'needs_interaction': True}

    try:
        proxy = proxy_options(proxy_url)
        with (playwright_factory or sync_playwright)() as pw:
            browser = pw.chromium.launch(
                executable_path=os.getenv('OAUTH_BROWSER_EXECUTABLE', '/usr/bin/chromium'),
                headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'], proxy=proxy)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(3000)

                def observe(request):
                    target = urlsplit(request.url)
                    if (target.scheme, target.netloc, target.path or '/') == (callback.scheme, callback.netloc, callback.path or '/'):
                        captured.update(parse_qs(target.query))

                page.on('request', observe)

                def intercept(route):
                    target = urlsplit(route.request.url)
                    if (target.scheme, target.netloc, target.path or '/') == (callback.scheme, callback.netloc, callback.path or '/'):
                        params = parse_qs(target.query)
                        captured.update(params)
                        route.fulfill(status=200, content_type='text/html', body='Authorization received. You may close this page.')
                    else:
                        route.continue_()

                context.route('**/*', intercept)
                page.goto(authorize_url, wait_until='domcontentloaded', timeout=30000)
                deadline = time.monotonic() + timeout_seconds
                submitted_email = submitted_password = False
                clicks = 0
                while not captured and time.monotonic() < deadline:
                    host = urlsplit(page.url).hostname
                    if host not in LOGIN_HOSTS:
                        return fail('浏览器进入了不支持的登录页面，需要人工处理')
                    challenge_field = page.locator('input[name="otc"], input[name="otcInput"], input[name="proofConfirmation"], input[autocomplete="one-time-code"], iframe[src*="captcha"], #hipBfpContainer').first
                    if challenge_field.is_visible():
                        return fail('微软要求验证码、MFA 或安全验证，需要人工处理')
                    error = page.locator('#passwordError, #usernameError, #idDiv_SAOTCS_Error, #errorDescription').first
                    if error.is_visible() and error.inner_text().strip():
                        return fail('微软登录失败，请检查密码或账号验证要求')
                    email_field = page.locator('input[name="loginfmt"]').first
                    password_field = page.locator('input[name="passwd"]').first
                    if email_field.is_visible() and not submitted_email:
                        email_field.fill(email)
                        page.locator('#idSIButton9, input[type="submit"], button[type="submit"]').first.click()
                        submitted_email = True
                    elif password_field.is_visible() and not submitted_password:
                        password_field.fill(password)
                        page.locator('#idSIButton9, input[type="submit"], button[type="submit"]').first.click()
                        submitted_password = True
                    elif submitted_password and clicks < 5:
                        # Microsoft consent / stay-signed-in only; never click
                        # generic buttons on unrelated or security challenge pages.
                        consent = page.locator('#idBtn_Accept, #idSIButton9').first
                        if consent.is_visible():
                            consent.click()
                            clicks += 1
                    page.wait_for_timeout(500)
                if captured.get('state') != [state]:
                    return fail('未收到有效授权回调，可能需要额外验证或登录页面已变化')
                if captured.get('error') or not captured.get('code'):
                    return fail('微软未批准授权，请检查应用权限或手动授权')
            finally:
                browser.close()
        data = {'grant_type': 'authorization_code', 'client_id': client_id,
                'redirect_uri': redirect_uri, 'code': captured['code'][0], 'code_verifier': verifier}
        with requests.Session() as session:
            session.trust_env = False
            if proxy_url and proxy_url != 'direct':
                session.proxies.update({'http': proxy_url, 'https': proxy_url})
            response = (exchange or session.post)(
                'https://login.microsoftonline.com/consumers/oauth2/v2.0/token', data=data, timeout=30)
            payload = response.json()
            if response.status_code != 200 or not payload.get('refresh_token'):
                return fail('授权码换取令牌失败，旧令牌未修改')
            return {'success': True, 'client_id': client_id, 'refresh_token': payload['refresh_token']}
    except ValueError as exc:
        return fail(str(exc) if '代理' in str(exc) else '浏览器授权响应无效')
    except Exception:
        # Browser exceptions often embed URLs containing authorization codes.
        return fail('浏览器授权未完成：请检查浏览器运行环境、代理连接或使用手动授权')
