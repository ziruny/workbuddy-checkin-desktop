"""WorkBuddy 签到 · 桌面面板。

四件事：
  1. 扫码登录 —— 用官方登录页扫码，账号各自保存自己的令牌，登录态互相隔离；
  2. 多账号   —— 想加几个号就加几个号，一个账号一份会话文件，删一个不动其它；
  3. 一键签到 —— 点一下按顺序把所有账号（或单个账号）签一遍，结果就地更新；
  4. 定时签到 —— 面板开着就每天定时自动签；勾选开机自启后，开机即待命。

签到逻辑一行没重写，全部走仓库根目录的 signin.py（见 _runner.py）：
接口失效时只需更新 signin.py，面板不用动。
"""

import ctypes
import datetime
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import store  # noqa: E402
import wb_auth  # noqa: E402

try:
    import signin  # 仅用于定位本机客户端凭据文件（导入已有账号）
except Exception:  # pragma: no cover
    signin = None

import webview  # noqa: E402

RUNNER = os.path.join(HERE, "_runner.py")
FROZEN = bool(getattr(sys, "frozen", False))
# 打包成 exe 后，ui/ 与引擎都塞在 PyInstaller 的解包目录里
RES_DIR = getattr(sys, "_MEIPASS", HERE)
UI_INDEX = os.path.join(RES_DIR, "ui", "index.html")
APP_TITLE = "WorkBuddy 签到助手"
SINGLE_INSTANCE_PORT = 51234
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "WorkBuddySignin"
LAUNCH_AGENT_ID = "com.workbuddy.signin"
LAUNCH_AGENT = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LAUNCH_AGENT_ID + ".plist")
ACCOUNT_TIMEOUT = 360
IS_MAC = sys.platform == "darwin"
LOG = logging.getLogger("wb-signin")


# ---------------------------------------------------------------- 小工具

def child_python():
    """子进程用的解释器：pythonw 没有 stdout，必须换成同目录的 python.exe。"""
    exe = sys.executable or ""
    if os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe or "python"


def window_python():
    """开机自启用 pythonw，避免每次开机弹一个黑框；打包后就是自身 exe。"""
    if FROZEN:
        return sys.executable or ""
    exe = sys.executable or ""
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return exe


def setup_logging():
    store.ensure_dirs()
    handler = logging.FileHandler(store.LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def fmt_day(ms):
    if not ms:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
    except (OSError, OverflowError, ValueError):
        return ""


def expires_state(ms):
    if not ms:
        return ""
    left = ms / 1000 - time.time()
    if left <= 0:
        return "expired"
    if left < 3 * 86400:
        return "soon"
    return "ok"


def _launch_agent_program():
    """自启时执行的命令：打包后是 .app 内的可执行文件，源码模式是 python + app.py。"""
    if FROZEN:
        return [os.path.abspath(sys.executable)]
    return [window_python(), os.path.abspath(__file__)]


def _write_launch_agent(enabled):
    if not enabled:
        try:
            subprocess.run(["launchctl", "unload", LAUNCH_AGENT], capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            os.remove(LAUNCH_AGENT)
        except OSError:
            pass
        return True
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    args = "".join("\n      <string>%s</string>" % p for p in _launch_agent_program())
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>%s</string>
    <key>ProgramArguments</key>
    <array>%s
    </array>
    <key>RunAtLoad</key>
    <true/>
  </dict>
</plist>
""" % (LAUNCH_AGENT_ID, args)
    with open(LAUNCH_AGENT, "w", encoding="utf-8") as f:
        f.write(plist)
    try:
        subprocess.run(["launchctl", "load", LAUNCH_AGENT], capture_output=True, timeout=10)
    except Exception:
        pass
    return True


def apply_autostart(enabled):
    if IS_MAC:
        try:
            return _write_launch_agent(enabled)
        except Exception as e:  # pragma: no cover
            LOG.warning("设置开机自启失败: %s", e)
            return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                if FROZEN:
                    cmd = '"%s"' % os.path.abspath(sys.executable)
                else:
                    cmd = '"%s" "%s"' % (window_python(), os.path.abspath(__file__))
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:  # pragma: no cover
        LOG.warning("设置开机自启失败: %s", e)
        return False


def autostart_enabled():
    if IS_MAC:
        return os.path.exists(LAUNCH_AGENT)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except Exception:
        return False


def parse_hhmm(text):
    hh, _, mm = str(text or "00:05").partition(":")
    try:
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except (TypeError, ValueError):
        return 0, 5


def next_run_text(settings):
    if not settings["enabled"]:
        return {"at": 0, "text": "定时已关闭"}
    hh, mm = parse_hhmm(settings["time"])
    now = datetime.datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    ran_today = settings.get("last_run_date") == store.local_day()
    if now >= target or ran_today:
        target += datetime.timedelta(days=1)
    if target.date() == now.date():
        text = "今天 %02d:%02d" % (hh, mm)
    elif target.date() == (now + datetime.timedelta(days=1)).date():
        text = "明天 %02d:%02d" % (hh, mm)
    else:
        text = "%s %02d:%02d" % (target.strftime("%m-%d"), hh, mm)
    return {"at": int(target.timestamp() * 1000), "text": "下次 " + text}


# ---------------------------------------------------------------- 应用逻辑

class Api:
    def __init__(self):
        self._window = None
        self._busy = False
        self._busy_lock = threading.Lock()
        self._alive = True
        self._login_window = None
        self._login_stop = None
        self._login_url = ""
        self._login_active = False

    # ------------------------------------------------------------ 推送

    def _push(self, event):
        if not self._window:
            return
        try:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            self._window.evaluate_js("window.__ev && window.__ev(%s)" % payload)
        except Exception:
            pass  # 窗口已关闭

    # ------------------------------------------------------------ 视图模型

    def _public_account(self, item):
        kind = item.get("kind") or "qr"
        # 扫码账号看 refreshToken 的有效期（到期前会自动续期）；导入账号看 accessToken
        key_ms = (item.get("refresh_expires_at") or item.get("expires_at")) if kind == "qr" \
            else (item.get("expires_at") or 0)
        state = expires_state(key_ms)
        if kind == "qr" and key_ms:
            expires_note = "登录态有效至 %s（到期前自动续期）" % fmt_day(key_ms)
        elif kind == "qr":
            expires_note = "登录态长期有效（自动续期）"
        elif key_ms:
            expires_note = "客户端凭据有效至 %s" % fmt_day(key_ms)
        else:
            expires_note = ""
        return {
            "id": item["id"],
            "label": item.get("label") or store.default_label(
                item.get("nickname"), item.get("uin"), item["id"]),
            "nickname": item.get("nickname") or "",
            "uin": item.get("uin") or "",
            "uid": item["id"],
            "kind": kind,
            "expires_at": key_ms,
            "expires_text": fmt_day(key_ms),
            "expires_state": state,
            "expires_note": expires_note,
            "imported_at": item.get("imported_at") or 0,
            "last": item.get("last") or None,
        }

    def _state(self):
        index = store.load_index()
        settings = store.load_settings()
        settings["autostart"] = settings.get("autostart") or autostart_enabled()
        accounts = [self._public_account(a) for a in index["accounts"]]
        accounts.sort(key=lambda a: a["label"] or "")
        today = store.local_day()
        signed = 0
        for a in accounts:
            last = a["last"] or {}
            if last.get("result") in ("ALREADY", "CLAIMED") and store.local_day(last.get("at")) == today:
                signed += 1
        return {
            "accounts": accounts,
            "settings": settings,
            "next_run": next_run_text(settings),
            "today": today,
            "signed_today": signed,
            "total": len(accounts),
            "busy": self._busy,
            "login": {"active": self._login_active, "url": self._login_url},
            "last_run": index.get("last_run"),
            "data_dir": store.APP_DIR,
            "engine_ok": signin is not None,
        }

    def bootstrap(self):
        return self._state()

    def refresh(self):
        return self._state()

    # ------------------------------------------------------------ 扫码登录

    def start_login(self):
        if self._login_active:
            return {"ok": False, "message": "登录窗口已经打开了"}
        self._login_active = True
        self._login_stop = threading.Event()
        threading.Thread(target=self._login_worker, daemon=True).start()
        return {"ok": True, "message": "正在创建登录会话…"}

    def cancel_login(self):
        if self._login_stop is not None:
            self._login_stop.set()
        self._close_login_window()
        return {"ok": True}

    def _close_login_window(self):
        win, self._login_window = self._login_window, None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _login_worker(self):
        stop = self._login_stop
        try:
            state, auth_url = wb_auth.create_state()
            self._login_url = auth_url
            LOG.info("登录会话已创建")
            self._push({"type": "login_state", "url": auth_url,
                        "message": "请在弹出的窗口里用微信扫码"})
            self._open_login_window(auth_url)
            tokens = wb_auth.poll_token(state, stop_event=stop)
            info = {"uid": tokens.get("uid") or wb_auth.uid_from_token(tokens["accessToken"]),
                    "uin": tokens.get("uin") or "", "nickname": ""}
            try:  # 账号信息接口偶尔要等一两秒才就绪，取不到不等于登录失败
                info.update({k: v for k, v in wb_auth.fetch_account(state, tokens["accessToken"]).items()
                             if v})
            except wb_auth.LoginError as e:
                if not info["uid"]:
                    raise wb_auth.LoginError("登录成功但取不到账号信息（%s），请重试" % e)
                LOG.info("账号信息接口未就绪，用令牌内的 uid 兜底: %s", e)
            item, created = store.save_qr_login(tokens, info)
            LOG.info("扫码登录成功 uid=%s created=%s", info["uid"], created)
            account = self._public_account(item)
            self._push({"type": "login_done", "account": account, "created": created})
        except wb_auth.LoginCancelled:
            self._push({"type": "login_failed", "cancelled": True, "message": "已取消登录"})
        except wb_auth.LoginError as e:
            LOG.warning("扫码登录失败: %s", e)
            self._push({"type": "login_failed", "message": str(e)})
        except Exception as e:
            LOG.warning("扫码登录异常: %s", traceback.format_exc())
            self._push({"type": "login_failed", "message": "登录异常（%s: %s）" % (type(e).__name__, e)})
        finally:
            self._close_login_window()
            self._login_active = False
            self._login_url = ""

    def _open_login_window(self, auth_url):
        """在应用内开一个独立窗口显示官方登录页；开不出来就退回系统浏览器。

        窗口用 private 模式：不落 cookie，两个账号之间不会互相污染登录态。
        """
        try:
            win = webview.create_window(
                "登录 WorkBuddy · 微信扫码",
                url=auth_url,
                width=520,
                height=720,
                background_color="#ffffff",
            )
            self._login_window = win
            try:
                win.events.closed += self._on_login_window_closed
            except Exception:
                pass
        except Exception as e:
            LOG.warning("应用内登录窗口创建失败，改用浏览器: %s", e)
            self._push({"type": "login_browser", "url": auth_url,
                        "message": "已在浏览器打开登录页，扫码后会自动添加"})
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass

    def _on_login_window_closed(self):
        # 用户手动关掉窗口 → 等同取消，别让后台一直等到 5 分钟超时
        if self._login_active and self._login_stop is not None:
            self._login_stop.set()

    def open_login_url(self):
        if self._login_url:
            try:
                webbrowser.open(self._login_url)
            except Exception:
                pass
        return {"ok": bool(self._login_url)}

    # ------------------------------------------------------------ 客户端导入

    def import_account(self):
        if signin is None:
            return {"ok": False, "message": "未找到 signin.py，无法读取本机登录凭据"}
        path, _looked = signin.find_auth_file()
        if not path:
            return {"ok": False, "message": "未找到客户端登录凭据。请先在 WorkBuddy 桌面端登录，再回来导入。"}
        try:
            session = signin.load_session_retry(path)
            record, created = store.save_client_import(session)
        except PermissionError:
            return {"ok": False, "message": "凭据文件被客户端占用，请稍等两秒再点一次"}
        except ValueError as e:
            return {"ok": False, "message": "凭据内容无法识别（%s）" % e}
        except Exception as e:
            LOG.warning("导入账号失败: %s", traceback.format_exc())
            return {"ok": False, "message": "导入失败（%s: %s）" % (type(e).__name__, e)}
        state = self._state()
        state["ok"] = True
        state["message"] = ("已导入账号 %s" % record.get("label")) if created else (
            "已更新账号 %s 的登录状态" % record.get("label"))
        return state

    def remove_account(self, account_id):
        store.remove(account_id)
        return self._state()

    def rename_account(self, account_id, label):
        store.rename(account_id, label)
        return self._state()

    def open_data_dir(self):
        try:
            if IS_MAC:
                subprocess.Popen(["open", store.APP_DIR])
            elif os.name == "nt":
                os.startfile(store.APP_DIR)  # noqa: S606 - 打开自己的数据目录
            else:
                subprocess.Popen(["xdg-open", store.APP_DIR])
        except Exception:
            pass
        return {"ok": True}

    # ------------------------------------------------------------ 设置

    def save_settings(self, patch):
        patch = dict(patch or {})
        if "autostart" in patch:
            patch["autostart"] = bool(patch["autostart"])
            apply_autostart(patch["autostart"])
        if "enabled" in patch:
            patch["enabled"] = bool(patch["enabled"])
        if "time" in patch:
            patch["last_run_date"] = ""
        store.save_settings(patch)
        return self._state()

    # ------------------------------------------------------------ 签到

    def signin(self, account_ids=None):
        index = store.load_index()
        if account_ids:
            wanted = [i for i in account_ids if store.find(index, i)]
        else:
            wanted = [a["id"] for a in index["accounts"]]
        if not wanted:
            return {"ok": False, "message": "还没有账号，先扫码添加一个"}
        if not self._start(wanted, trigger="manual"):
            return {"ok": False, "message": "正在签到中，请稍候"}
        return {"ok": True, "count": len(wanted)}

    def _start(self, ids, trigger):
        with self._busy_lock:
            if self._busy:
                return False
            self._busy = True
        threading.Thread(target=self._signin_worker, args=(ids, trigger), daemon=True).start()
        return True

    def _run_one(self, account_id):
        if FROZEN:
            # 打包后不再另起进程：同一个 exe 再启动一次要重新解包几十兆，慢且无必要
            return self._run_inprocess(account_id)
        env = os.environ.copy()
        env.pop("WORKBUDDY_AUTH_FILE", None)
        env["SIGNIN_APP_UID"] = account_id
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [child_python(), RUNNER],
                env=env,
                capture_output=True,
                timeout=ACCOUNT_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return {"result": "TIMEOUT", "needs_attention": True,
                    "report": "签到超过 %d 分钟未返回，已中止" % (ACCOUNT_TIMEOUT // 60)}
        except Exception as e:
            return {"result": "ERROR", "needs_attention": True,
                    "report": "无法启动签到进程（%s: %s）" % (type(e).__name__, e)}

        out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        for line in reversed([l for l in out.splitlines() if l.strip()]):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("result"):
                return data
        LOG.warning("账号 %s 无有效输出 rc=%s stderr=%s", account_id, proc.returncode, err[-800:])
        return {"result": "ERROR", "needs_attention": True,
                "report": "签到进程没有返回结果（退出码 %s）" % proc.returncode}

    def _run_inprocess(self, account_id):
        """冻结版走进程内执行：signin.py 自带时间预算，不会永久卡住线程。"""
        try:
            import _runner
            out = _runner.run(account_id)
            if isinstance(out, dict) and out.get("result"):
                return out
            return {"result": "ERROR", "needs_attention": True, "report": "签到没有返回结果"}
        except Exception as e:
            LOG.warning("账号 %s 进程内签到异常: %s", account_id, traceback.format_exc())
            return {"result": "ERROR", "needs_attention": True,
                    "report": "签到异常（%s: %s）" % (type(e).__name__, e)}

    def _signin_worker(self, ids, trigger):
        summary = {"at": int(time.time()), "trigger": trigger, "claimed": 0,
                   "already": 0, "failed": 0, "total": len(ids)}
        try:
            self._push({"type": "run_start", "ids": ids, "trigger": trigger})
            for account_id in ids:
                self._push({"type": "account_start", "id": account_id})
                out = self._run_one(account_id)
                updated = store.record_result(account_id, out)
                if out.get("result") == "CLAIMED":
                    summary["claimed"] += 1
                elif out.get("result") == "ALREADY":
                    summary["already"] += 1
                elif out.get("result") == "INACTIVE":
                    pass
                else:
                    summary["failed"] += 1
                self._push({"type": "account_done", "id": account_id,
                            "account": self._public_account(updated) if updated else None,
                            "result": out.get("result"), "report": out.get("report") or ""})
            parts = []
            if summary["claimed"]:
                parts.append("%d 个领取成功" % summary["claimed"])
            if summary["already"]:
                parts.append("%d 个今日已签" % summary["already"])
            if summary["failed"]:
                parts.append("%d 个失败" % summary["failed"])
            summary["text"] = "、".join(parts) or "没有可签到的账号"
            store.record_run(summary)
            self._push({"type": "run_done", "summary": summary})
            LOG.info("签到完成 %s", summary["text"])
        except Exception:
            LOG.warning("签到线程异常: %s", traceback.format_exc())
            self._push({"type": "run_done", "summary": dict(summary, text="运行异常，详见日志")})
        finally:
            with self._busy_lock:
                self._busy = False
            self._push({"type": "idle"})

    # ------------------------------------------------------------ 定时

    def start_scheduler(self):
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    def _scheduler_loop(self):
        while self._alive:
            try:
                self._schedule_tick()
            except Exception:
                LOG.warning("定时检查异常: %s", traceback.format_exc())
            for _ in range(10):
                if not self._alive:
                    return
                time.sleep(1)

    def _schedule_tick(self):
        settings = store.load_settings()
        if not settings["enabled"]:
            return
        today = store.local_day()
        if settings.get("last_run_date") == today:
            return
        hh, mm = parse_hhmm(settings["time"])
        now = datetime.datetime.now()
        if now < now.replace(hour=hh, minute=mm, second=0, microsecond=0):
            return
        ids = [a["id"] for a in store.load_index()["accounts"]]
        # 先记日期再跑：失败也不该每 10 秒重来，把服务端打成异常流量
        store.save_settings({"last_run_date": today})
        if not ids:
            return
        if self._start(ids, trigger="schedule"):
            LOG.info("定时签到触发，%d 个账号", len(ids))

    def stop(self):
        self._alive = False

    def on_main_closed(self):
        """关窗口就退出。

        macOS 上关掉最后一个窗口后进程有时还赖着不走，会一直占住单实例锁，
        下次双击就变成"没反应"。这里主动收摊，行为与 Windows 一致。
        """
        self._alive = False
        threading.Timer(0.8, lambda: os._exit(0)).start()


# ---------------------------------------------------------------- 启动

def already_running():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        sock.close()
        return True
    globals()["_INSTANCE_SOCKET"] = sock  # 进程退出时由系统回收
    return False


def run_signin_cli(argv):
    """自检/调试入口：--run-signin <uid> [--out 结果文件]。"""
    uid, out_path = "", os.environ.get("SIGNIN_APP_OUT") or ""
    for i, arg in enumerate(argv):
        if arg == "--run-signin" and i + 1 < len(argv):
            uid = argv[i + 1]
        elif arg == "--out" and i + 1 < len(argv):
            out_path = argv[i + 1]
    import _runner
    payload = json.dumps(_runner.run(uid), ensure_ascii=False, default=str)
    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(payload)
        except OSError:
            pass
    try:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


def main():
    if "--run-signin" in sys.argv:
        return run_signin_cli(sys.argv)
    setup_logging()
    store.ensure_dirs()
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    if already_running():
        LOG.info("已有实例在运行，退出")
        return 0

    api = Api()
    window = webview.create_window(
        APP_TITLE,
        url=UI_INDEX,
        js_api=api,
        width=980,
        height=760,
        min_size=(780, 600),
        background_color="#0b0d12",
    )
    api._window = window
    api.start_scheduler()
    try:
        window.events.closed += api.on_main_closed
    except Exception:
        pass
    try:
        webview.start(debug=bool(os.environ.get("WB_SIGNIN_DEBUG")))
    finally:
        api.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
