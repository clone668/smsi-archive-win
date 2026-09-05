from __future__ import annotations

import base64
import binascii
import ctypes
import os
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(payload: bytes) -> tuple[DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(payload)
    return DATA_BLOB(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_password(password: str) -> str:
    if os.name != "nt":
        raise RuntimeError("密码保护仅支持 Windows DPAPI")
    raw_blob, raw_buffer = _blob(password.encode("utf-8"))
    protected = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID, ctypes.POINTER(DATA_BLOB), wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    protect.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not protect(
        ctypes.byref(raw_blob),
        "SMSI Archive Sync" ,
        None,
        None,
        None,
        0,
        ctypes.byref(protected),
    ):
        raise ctypes.WinError()
    try:
        payload = ctypes.string_at(protected.pbData, protected.cbData)
        return "dpapi:v1:" + base64.b64encode(payload).decode("ascii")
    finally:
        kernel32.LocalFree(protected.pbData)
        del raw_buffer


def unprotect_password(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("dpapi:v1:"):
        raise RuntimeError("密码配置不是 Windows DPAPI 格式")
    if os.name != "nt":
        raise RuntimeError("密码保护仅支持 Windows DPAPI")
    encoded = value.removeprefix("dpapi:v1:")
    try:
        encrypted = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("密码配置损坏") from exc
    encrypted_blob, encrypted_buffer = _blob(encrypted)
    plain = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, ctypes.POINTER(DATA_BLOB),
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    unprotect.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not unprotect(
        ctypes.byref(encrypted_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(plain),
    ):
        raise ctypes.WinError()
    try:
        payload = ctypes.string_at(plain.pbData, plain.cbData)
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("密码配置无法解密") from exc
    finally:
        kernel32.LocalFree(plain.pbData)
        del encrypted_buffer
