"""WorkBuddy 扫码登录（官方 plugin 接口）与登录态续期。

流程与官方客户端的「浏览器扫码登录」一致：
  POST {endpoint}/v2/plugin/auth/state?platform=workbuddy   → state + authUrl（扫码页）
  GET  {endpoint}/v2/plugin/auth/token?state=<state>        → 扫码确认后返回令牌（未确认 11217）
  GET  {endpoint}/v2/plugin/login/account?state=<state>     → 账号 uid / 昵称（未就绪 12151）
  POST {endpoint}/v2/plugin/auth/token/refresh              → 用 refreshToken 换新令牌

每个账号的令牌各存一份，互不共享；二维码由官方页面渲染，本程序不接触微信信息。
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://www.workbuddy.cn"
PENDING_TOKEN = 11217
PENDING_ACCOUNT = 12151
POLL_INTERVAL = 2.0
LOGIN_TIMEOUT = 300.0
REFRESH_MARGIN = 24 * 3600.0   # 剩余不足 1 天就提前换新

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "WorkBuddy",
}


class LoginError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class LoginCancelled(Exception):
    pass


def _call(method, path, payload=None, token=None, uid=None, domain=None, extra=None, timeout=20):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if uid:
        headers["X-User-Id"] = uid
    if domain:
        headers["X-Domain"] = domain
    if extra:
        headers.update(extra)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    url = ENDPOINT + path if path.startswith("/") else path
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        raise LoginError("网络请求失败：%s" % e) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise LoginError("服务端返回了无法解析的内容（HTTP %s）" % status) from None
    return status, data if isinstance(data, dict) else {}


def create_state():
    """申请一次登录会话，返回 (state, auth_url)。"""
    status, body = _call("POST", "/v2/plugin/auth/state?platform=workbuddy", payload={})
    data = body.get("data") or {}
    state = data.get("state")
    url = data.get("authUrl") or data.get("auth_url")
    if status != 200 or not state or not url:
        raise LoginError("无法创建登录会话：%s（HTTP %s）" % (body.get("msg") or "未知原因", status),
                         code=body.get("code"))
    return state, url


def _expire_ms(value):
    """服务端有的字段给毫秒，有的给秒，统一成毫秒。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0
    if not value:
        return 0
    return int(value * 1000) if value < 1e11 else int(value)


def normalize_tokens(data):
    """把登录/续期响应整理成统一的令牌结构（缺字段的接口各自不同）。"""
    if not isinstance(data, dict):
        return None
    access = data.get("accessToken") or data.get("access_token")
    if not access:
        return None
    now_ms = int(time.time() * 1000)
    expires_at = _expire_ms(data.get("expiresAt") or data.get("expires_at"))
    if not expires_at:
        try:  # expiresIn 是相对秒数
            seconds = float(data.get("expiresIn") or data.get("expires_in") or 0)
            expires_at = now_ms + int(seconds * 1000) if seconds > 0 else 0
        except (TypeError, ValueError):
            expires_at = 0
    refresh_expires = _expire_ms(data.get("refreshExpiresAt") or data.get("refresh_expires_at"))
    if not refresh_expires:
        try:
            seconds = float(data.get("refreshExpiresIn") or data.get("refresh_expires_in") or 0)
            refresh_expires = now_ms + int(seconds * 1000) if seconds > 0 else 0
        except (TypeError, ValueError):
            refresh_expires = 0
    return {
        "accessToken": access,
        "refreshToken": data.get("refreshToken") or data.get("refresh_token") or "",
        "tokenType": data.get("tokenType") or data.get("token_type") or "Bearer",
        "expiresAt": expires_at,
        "refreshExpiresAt": refresh_expires,
        "domain": data.get("domain") or "",
        "uid": str(data.get("uid") or data.get("userId") or data.get("user_id") or ""),
        "uin": str(data.get("uin") or ""),
    }


def uid_from_token(token):
    """令牌若是 JWT，从载荷里兜底取 uid（账号信息接口偶发未就绪时用）。"""
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode("ascii")).decode("utf-8"))
    except Exception:
        return ""
    for key in ("sub", "uid", "userId", "user_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def poll_token(state, stop_event=None, deadline=None, on_tick=None):
    """等待扫码确认，返回令牌。未确认时按 2 秒轮询。"""
    end = deadline or (time.time() + LOGIN_TIMEOUT)
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            raise LoginCancelled()
        status, body = _call("GET", "/v2/plugin/auth/token?state=%s" % urllib.parse.quote(state))
        code = body.get("code")
        tokens = normalize_tokens(body.get("data") or {})
        if tokens and status == 200:
            return tokens
        if code not in (PENDING_TOKEN, 0, None) or status >= 500:
            raise LoginError(body.get("msg") or "获取登录令牌失败（HTTP %s）" % status, code=code)
        if on_tick:
            on_tick()
        for _ in range(int(POLL_INTERVAL * 2)):
            if stop_event is not None and stop_event.is_set():
                raise LoginCancelled()
            time.sleep(0.5)
    raise LoginError("等待扫码超时，请重新发起登录")


def fetch_account(state, token, timeout=30):
    """登录后取账号信息（uid 必填，昵称取不到就给空）。"""
    end = time.time() + timeout
    while time.time() < end:
        status, body = _call("GET", "/v2/plugin/login/account?state=%s" % urllib.parse.quote(state),
                             token=token)
        data = body.get("data") or {}
        uid = data.get("uid") or data.get("userId") or data.get("user_id")
        if uid:
            nick = data.get("nickname") or data.get("nickName") or ""
            if isinstance(nick, dict):
                nick = nick.get("value") or ""
            return {"uid": str(uid), "uin": str(data.get("uin") or ""), "nickname": str(nick or "")}
        if body.get("code") not in (PENDING_ACCOUNT, 0, None):
            raise LoginError(body.get("msg") or "获取账号信息失败", code=body.get("code"))
        time.sleep(POLL_INTERVAL)
    raise LoginError("获取账号信息超时")


def refresh(tokens, uid=None):
    """用 refreshToken 换一对新令牌。"""
    refresh_token = (tokens or {}).get("refreshToken")
    if not refresh_token:
        raise LoginError("没有 refreshToken，需要重新扫码登录")
    extra = {"X-Refresh-Token": refresh_token, "X-Auth-Refresh-Source": "plugin"}

    def attempt(with_token):
        return _call("POST", "/v2/plugin/auth/token/refresh", payload={},
                     token=tokens.get("accessToken") if with_token else None,
                     uid=uid, domain=tokens.get("domain"), extra=extra)

    try:
        status, body = attempt(True)
    except LoginError:
        status, body = 0, {}
    if status in (401, 403):
        status, body = attempt(False)
    fresh = normalize_tokens(body.get("data") or {})
    if status == 200 and fresh:
        if not fresh.get("refreshToken"):
            fresh["refreshToken"] = refresh_token
        return fresh
    code = body.get("code")
    if code == 12153 or "invalid_grant" in str(body.get("msg") or ""):
        raise LoginError("登录态已被服务端作废，请重新扫码登录该账号", code=code)
    raise LoginError(body.get("msg") or "续期失败（HTTP %s）" % status, code=code)


def needs_refresh(tokens, margin=REFRESH_MARGIN):
    expires = (tokens or {}).get("expiresAt") or 0
    if not expires:
        return False
    return (expires / 1000.0 - time.time()) < margin


def build_session(tokens, account, endpoint=ENDPOINT):
    """整理成 signin.py 认识的会话结构（明文令牌，不再有 $wbEncrypted 信封）。"""
    auth = {
        "accessToken": tokens["accessToken"],
        "refreshToken": tokens.get("refreshToken") or "",
        "tokenType": tokens.get("tokenType") or "Bearer",
        "expiresAt": tokens.get("expiresAt") or 0,
        "refreshExpiresAt": tokens.get("refreshExpiresAt") or 0,
        "domain": tokens.get("domain") or "",
        "endpoint": endpoint,
        "lastRefreshTime": int(time.time() * 1000),
    }
    acc = {"uid": account["uid"], "uin": account.get("uin") or ""}
    if account.get("nickname"):
        acc["nickname"] = account["nickname"]
    return {"auth": auth, "account": acc}
