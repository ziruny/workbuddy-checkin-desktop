"""令牌落地保护。

扫码登录拿到的是明文 accessToken / refreshToken，落到磁盘的东西等于账号本身，
因此 Windows 上用 DPAPI（CryptProtectData）绑当前 Windows 用户加密，换机/换用户都解不开。
macOS / Linux 没有等价的标准库接口，退回明文，但文件写入时收紧到 600，
且位置在用户私有目录（macOS 为 ~/Library/Application Support）。
"""

import base64
import ctypes
import json
import sys

_IS_WIN = sys.platform == "win32"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(func, data):
    src, keep = _blob(data)
    out = _Blob()
    ok = func(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text):
    """返回可 JSON 序列化的密文结构。"""
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if not _IS_WIN:
        return {"kind": "plain", "blob": base64.b64encode(raw).decode("ascii")}
    try:
        enc = _dpapi(ctypes.windll.crypt32.CryptProtectData, raw)
        return {"kind": "dpapi", "blob": base64.b64encode(enc).decode("ascii")}
    except Exception:
        return {"kind": "plain", "blob": base64.b64encode(raw).decode("ascii")}


def unprotect(box):
    blob = base64.b64decode((box or {}).get("blob") or "")
    if (box or {}).get("kind") == "dpapi":
        return _dpapi(ctypes.windll.crypt32.CryptUnprotectData, blob).decode("utf-8")
    return blob.decode("utf-8")


def pack_session(session):
    return protect(json.dumps(session, ensure_ascii=False))


def unpack_session(box):
    return json.loads(unprotect(box))
