"""按账号执行一次签到，把结果打成单行 JSON 交给桌面端。

由 app.py 以子进程方式调用：每个账号一个独立进程，互不共享时间预算和全局状态。
签到逻辑复用仓库根目录的 signin.py；本文件只多做两件事：
  1. 令牌快过期时先用 refreshToken 换新（登录一次可长期使用）；
  2. 服务端返回 401/403 时强制续期再试一次。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import signin  # noqa: E402
import store  # noqa: E402
import wb_auth  # noqa: E402


def _tokens_of(session):
    auth = (session or {}).get("auth") or {}
    return {
        "accessToken": auth.get("accessToken") or "",
        "refreshToken": auth.get("refreshToken") or "",
        "expiresAt": int(auth.get("expiresAt") or 0),
        "refreshExpiresAt": int(auth.get("refreshExpiresAt") or 0),
        "domain": auth.get("domain") or "",
    }


def _headers(session):
    resolved = signin.resolve_session(session)
    headers = signin.build_headers(resolved)
    endpoint = ((resolved.get("auth") or {}).get("endpoint") or signin.DEFAULT_ENDPOINT).rstrip("/")
    return headers, endpoint


def _refresh(uid, session):
    tokens = _tokens_of(session)
    if not tokens.get("refreshToken"):
        return session, "没有 refreshToken"
    try:
        fresh = wb_auth.refresh(tokens, uid=uid)
    except wb_auth.LoginError as e:
        return session, str(e)
    store.update_tokens(uid, fresh)
    session, _kind = store.read_cred(uid)
    return session, None


def run(uid):
    session, kind = store.read_cred(uid)
    if not session:
        return {"result": "NO_AUTH", "needs_attention": True,
                "report": "登录状态文件缺失或损坏，请重新扫码登录该账号"}
    note = []
    if kind == "qr":
        if wb_auth.needs_refresh(_tokens_of(session)):
            session, err = _refresh(uid, session)
            if err:
                note.append("续期失败：%s" % err)
            else:
                note.append("已自动续期登录态")
    try:
        headers, endpoint = _headers(session)
    except signin.AuthError as e:
        out = e.output()
        if note:
            out["report"] = out["report"] + "；" + "；".join(note)
        return out

    signin._start_budget("auto")
    try:
        code, out = signin.run_auto(headers, endpoint)
    except Exception as e:
        out = {"result": "ERROR", "needs_attention": True,
               "report": "签到异常（%s: %s）" % (type(e).__name__, e)}
        code = 1

    # 服务端说登录态不行，先别急着让用户重新扫码：refreshToken 通常还能救一次
    if kind == "qr" and out.get("result") in ("AUTH_REJECTED", "FORBIDDEN"):
        session, err = _refresh(uid, session)
        if not err:
            try:
                headers, endpoint = _headers(session)
                signin._start_budget("auto")
                code, out = signin.run_auto(headers, endpoint)
                note.append("登录态已续期后重试")
            except (signin.AuthError, Exception):
                pass

    if note:
        out["report"] = (out.get("report") or "") + "；" + "；".join(note)
    out["exit"] = code
    return out


def main():
    uid = os.environ.get("SIGNIN_APP_UID") or (sys.argv[1] if len(sys.argv) > 1 else "")
    out = run(uid)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
