"""账号与设置的本地存储。

一个账号 = 一份独立会话文件（creds/<uid>.json）+ 索引里的一条记录。
会话文件里要么是扫码登录拿到的明文令牌（DPAPI 加密后落盘），要么是从客户端
导入的加密信封原件；两者对 signin.py 都是合法会话，互不影响、互不共享。

会话按账号隔离：切换/删除某个账号，不会动到其它账号的登录态。
"""

import json
import os
import sys
import time
import uuid

import credbox

APP_NAME = "WorkBuddySignin"


def _data_dir():
    """各平台放配置的常规位置：Windows %APPDATA%，macOS Application Support，其它 XDG。"""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", APP_NAME)
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or home, APP_NAME)
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), APP_NAME)


APP_DIR = _data_dir()
CRED_DIR = os.path.join(APP_DIR, "creds")
INDEX_PATH = os.path.join(APP_DIR, "accounts.json")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "app.log")

DEFAULT_SETTINGS = {
    "enabled": False,     # 每日自动签到
    "time": "00:05",      # 自动签到时刻（本地时间）
    "autostart": False,   # 开机自启（HKCU Run）
    "last_run_date": "",  # 当天是否已经跑过
}


def ensure_dirs():
    os.makedirs(CRED_DIR, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    """先写临时文件再替换：断电/强杀时不会留下半截 JSON，把账号全弄丢。"""
    ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:  # 会话文件里有令牌，别让同机其它用户读到（Windows 上 ACL 已由用户目录隔离）
        os.chmod(path, 0o600)
    except OSError:
        pass


def local_day(stamp=None):
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


# ---------------------------------------------------------------- 设置

def load_settings():
    data = _read_json(SETTINGS_PATH, {})
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    hh, _, mm = str(merged.get("time") or "00:05").partition(":")
    try:
        merged["time"] = "%02d:%02d" % (max(0, min(23, int(hh))), max(0, min(59, int(mm))))
    except (TypeError, ValueError):
        merged["time"] = DEFAULT_SETTINGS["time"]
    merged["enabled"] = bool(merged.get("enabled"))
    merged["autostart"] = bool(merged.get("autostart"))
    return merged


def save_settings(patch):
    settings = load_settings()
    settings.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS})
    _write_json(SETTINGS_PATH, settings)
    return load_settings()


# ---------------------------------------------------------------- 索引

def load_index():
    data = _read_json(INDEX_PATH, {})
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    clean = [a for a in accounts if isinstance(a, dict) and a.get("id")]
    return {"version": 2, "accounts": clean, "last_run": data.get("last_run") or None}


def save_index(index):
    _write_json(INDEX_PATH, index)


def find(index, account_id):
    for item in index["accounts"]:
        if item["id"] == account_id:
            return item
    return None


def cred_path(uid):
    return os.path.join(CRED_DIR, "%s.json" % uid)


# ---------------------------------------------------------------- 会话

def write_cred(uid, session, kind):
    ensure_dirs()
    payload = {"v": 1, "kind": kind, "uid": uid,
               "saved_at": int(time.time()),
               "box": credbox.pack_session(session)}
    _write_json(cred_path(uid), payload)


def read_cred(uid):
    """返回 (session, kind)；文件缺失或损坏返回 (None, None)。"""
    data = _read_json(cred_path(uid), {})
    box = data.get("box")
    if not isinstance(box, dict):
        return None, None
    try:
        return credbox.unpack_session(box), data.get("kind") or "unknown"
    except Exception:
        return None, None


def default_label(nickname, uin, uid):
    if nickname:
        return nickname[:32]
    if uin and len(uin) >= 4:
        return "账号 %s" % uin[-4:]
    return "账号 %s" % (uid or "")[:6]


def _upsert(uid, session, kind, nickname=None, uin=None):
    index = load_index()
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    item = find(index, uid)
    fields = {
        "kind": kind,
        "endpoint": str(auth.get("endpoint") or "https://www.workbuddy.cn").rstrip("/"),
        "expires_at": int(auth.get("expiresAt") or 0),
        "refresh_expires_at": int(auth.get("refreshExpiresAt") or 0),
        "uin": str(uin or account.get("uin") or ""),
    }
    if nickname:
        fields["nickname"] = str(nickname)[:32]
    write_cred(uid, session, kind)
    if item:
        item.update(fields)
        item["updated_at"] = int(time.time())
        save_index(index)
        return item, False
    item = {
        "id": uid,
        "uid": uid,
        "imported_at": int(time.time()),
        "updated_at": int(time.time()),
        "last": None,
        "label": "",
    }
    item.update(fields)
    item["label"] = default_label(item.get("nickname"), item["uin"], uid)
    index["accounts"].append(item)
    index["accounts"].sort(key=lambda a: a.get("imported_at") or 0)
    save_index(index)
    return item, True


def save_qr_login(tokens, account_info, endpoint="https://www.workbuddy.cn"):
    """扫码登录成功：写会话 + 建/更新账号。"""
    import wb_auth
    session = wb_auth.build_session(tokens, account_info, endpoint)
    return _upsert(account_info["uid"], session, "qr",
                   nickname=account_info.get("nickname"), uin=account_info.get("uin"))


def save_client_import(session, source_path=None):
    """从本机客户端凭据导入：原样保存（信封仍是加密的）。"""
    account = session.get("account") or {}
    uid = str(account.get("uid") or "")
    if not uid:
        raise ValueError("凭据里没有账号标识（uid）")
    return _upsert(uid, session, "client")


def update_tokens(uid, tokens):
    """续期后回写：只改会话文件与索引里的有效期，不动账号其它信息。"""
    session, kind = read_cred(uid)
    if not session:
        return None
    auth = dict(session.get("auth") or {})
    auth.update({
        "accessToken": tokens.get("accessToken") or auth.get("accessToken"),
        "expiresAt": int(tokens.get("expiresAt") or auth.get("expiresAt") or 0),
        "lastRefreshTime": int(time.time() * 1000),
    })
    if tokens.get("refreshToken"):
        auth["refreshToken"] = tokens["refreshToken"]
    if tokens.get("refreshExpiresAt"):
        auth["refreshExpiresAt"] = int(tokens["refreshExpiresAt"])
    session = dict(session, auth=auth)
    write_cred(uid, session, kind or "qr")
    index = load_index()
    item = find(index, uid)
    if item:
        item["expires_at"] = auth.get("expiresAt") or 0
        item["refresh_expires_at"] = auth.get("refreshExpiresAt") or 0
        save_index(index)
    return item


def remove(account_id):
    index = load_index()
    kept = [a for a in index["accounts"] if a["id"] != account_id]
    if len(kept) == len(index["accounts"]):
        return False
    index["accounts"] = kept
    save_index(index)
    path = cred_path(account_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    return True


def rename(account_id, label):
    index = load_index()
    item = find(index, account_id)
    if not item:
        return None
    clean = (label or "").strip()[:32]
    item["label"] = clean or default_label(item.get("nickname"), item.get("uin"), account_id)
    save_index(index)
    return item


def record_result(account_id, out):
    index = load_index()
    item = find(index, account_id)
    if not item:
        return None
    item["last"] = {
        "at": int(time.time()),
        "result": out.get("result") or "UNKNOWN",
        "report": out.get("report") or "",
        "credit": out.get("credit"),
        "today_credit": out.get("today_credit"),
        "streak_days": out.get("streak_days"),
        "total_credits": out.get("total_credits"),
        "needs_attention": bool(out.get("needs_attention")),
    }
    save_index(index)
    return item


def record_run(summary):
    index = load_index()
    index["last_run"] = summary
    save_index(index)


def new_request_id():
    return uuid.uuid4().hex[:8]
