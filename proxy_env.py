from __future__ import annotations

import os
import sys
from collections.abc import Callable


PROXY_ENV_KEYS: tuple[str, ...] = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def _proxy_key_variants(name: str) -> tuple[str, str]:
    return name.upper(), name.lower()


def _proxy_value(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _read_windows_registry_env(scope: str, name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except Exception:
        return None

    if scope == "user":
        root = winreg.HKEY_CURRENT_USER
        subkey = "Environment"
    elif scope == "machine":
        root = winreg.HKEY_LOCAL_MACHINE
        subkey = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    else:
        return None

    try:
        with winreg.OpenKey(root, subkey) as key:
            for variant in _proxy_key_variants(name):
                try:
                    value, _value_type = winreg.QueryValueEx(key, variant)
                except FileNotFoundError:
                    continue
                proxy_value = _proxy_value(value)
                if proxy_value is not None:
                    return proxy_value
    except OSError:
        return None
    return None


def _read_proxy_env(
    name: str,
    *,
    registry_reader: Callable[[str, str], str | None],
) -> str | None:
    for variant in _proxy_key_variants(name):
        proxy_value = _proxy_value(os.environ.get(variant))
        if proxy_value is not None:
            return proxy_value
    for scope in ("user", "machine"):
        proxy_value = _proxy_value(registry_reader(scope, name))
        if proxy_value is not None:
            return proxy_value
    return None


def bootstrap_proxy_environment(
    *,
    registry_reader: Callable[[str, str], str | None] = _read_windows_registry_env,
) -> dict[str, str]:
    applied: dict[str, str] = {}
    for key in PROXY_ENV_KEYS:
        proxy_value = _read_proxy_env(key, registry_reader=registry_reader)
        if proxy_value is None:
            continue
        upper, lower = _proxy_key_variants(key)
        if _proxy_value(os.environ.get(upper)) is None:
            os.environ[upper] = proxy_value
            applied[upper] = proxy_value
        if _proxy_value(os.environ.get(lower)) is None:
            os.environ[lower] = proxy_value
            applied[lower] = proxy_value
    return applied
