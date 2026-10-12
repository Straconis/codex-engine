"""Protect API keys with Windows DPAPI for the current Windows user."""
import base64
import ctypes
import sys
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("Saving DeepSeek keys currently requires Windows key protection.")
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Could not unlock the saved API key. Re-enter it on this Windows account.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def protect(key: str) -> str:
    return base64.b64encode(_crypt(key.encode("utf-8"), False)).decode("ascii")


def unprotect(encrypted: str) -> str:
    try:
        return _crypt(base64.b64decode(encrypted, validate=True), True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise OSError("Could not unlock the saved API key. Re-enter it in Settings.") from exc
