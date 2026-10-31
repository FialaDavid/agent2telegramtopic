"""Secrets kept in the macOS Keychain instead of config.json (``token_keychain`` and friends).

A reference is ``{"service": str, "account": str (optional)}``. Reading uses
``security find-generic-password -s <service> [-a <account>] -w``. Errors name the Keychain item,
never a secret; every value read is registered with the log redaction.
"""
from __future__ import annotations

import subprocess
import sys

from . import redaction

_ITEM_NOT_FOUND = 44   # `security` exit status for errSecItemNotFound
#: `security add-generic-password` insists on an account; used when the config names only a service.
DEFAULT_ACCOUNT = "agent2telegramtopic"


def reference(raw: dict, key: str) -> tuple[str, str | None]:
    """Validate ``raw[key]`` as a Keychain reference; returns (service, account)."""
    value = raw[key]
    if (not isinstance(value, dict) or set(value) - {"service", "account"}
            or not isinstance(value.get("service"), str) or not value["service"].strip()
            or ("account" in value and (not isinstance(value["account"], str) or not value["account"].strip()))):
        raise ValueError(f"'{key}' must be an object like {{\"service\": \"name\", \"account\": \"optional\"}}")
    return value["service"], value.get("account")


def _item(service: str, account: str | None) -> list[str]:
    return ["-s", service, *(["-a", account] if account else [])]


def _name(service: str, account: str | None) -> str:
    return f"Keychain item '{service}'" + (f" (account '{account}')" if account else "")


def read(service: str, account: str | None = None, *, missing_ok: bool = False,
         platform: str | None = None, runner=None) -> str:
    """The secret stored in the Keychain; "" for a missing item when *missing_ok*."""
    platform, runner = platform or sys.platform, runner or subprocess.run
    name = _name(service, account)
    if platform != "darwin":
        raise ValueError(f"{name} cannot be read: the macOS Keychain is only available on macOS")
    try:
        result = runner(["security", "find-generic-password", *_item(service, account), "-w"],
                        capture_output=True, text=True)
    except OSError as exc:
        raise ValueError(f"{name} cannot be read: {exc.__class__.__name__}") from None
    if result.returncode == _ITEM_NOT_FOUND and missing_ok:
        return ""
    secret = (result.stdout or "").rstrip("\n") if result.returncode == 0 else ""
    if not secret:
        raise ValueError(f"{name} not found or unreadable (security exit status {result.returncode})")
    redaction.register_secret(secret)
    return secret


def write(service: str, account: str | None, secret: str, *, platform: str | None = None,
          runner=None) -> None:
    """Store (or update) the secret. It travels to ``security -i`` on stdin, so it never appears in a
    process listing (``-i`` passes a failed command's exit status on)."""
    platform, runner = platform or sys.platform, runner or subprocess.run
    account = account or DEFAULT_ACCOUNT
    name = _name(service, account)
    if platform != "darwin":
        raise ValueError(f"{name} cannot be written: the macOS Keychain is only available on macOS")
    if any(c in secret + service + account for c in "\r\n\0"):
        raise ValueError(f"{name} cannot be written: control characters in a value")

    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    command = " ".join(["add-generic-password", "-U", "-s", quote(service), "-a", quote(account),
                        "-w", quote(secret)])
    try:
        result = runner(["security", "-i"], input=command + "\n", capture_output=True, text=True)
    except OSError as exc:
        raise ValueError(f"{name} cannot be written: {exc.__class__.__name__}") from None
    if result.returncode != 0:
        raise ValueError(f"{name} could not be written (security exit status {result.returncode})")
    redaction.register_secret(secret)
