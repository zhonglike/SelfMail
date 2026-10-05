"""口令散列（scrypt）、会话令牌、登录失败自动封禁。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                           n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(h.hex(), hash_hex)
    except Exception:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


class RateLimiter:
    """按 IP 统计认证失败并临时封禁（内存态，重启即清）。"""

    def __init__(self, max_fails: int = 5, ban_minutes: int = 30):
        self.max_fails = max_fails
        self.ban_seconds = ban_minutes * 60
        self._fails: dict[str, list[float]] = {}
        self._banned: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_banned(self, ip: str) -> bool:
        with self._lock:
            until = self._banned.get(ip, 0)
            return time.time() < until

    def record_fail(self, ip: str):
        now = time.time()
        with self._lock:
            lst = [t for t in self._fails.get(ip, []) if now - t < 900]
            lst.append(now)
            self._fails[ip] = lst
            if len(lst) >= self.max_fails:
                self._banned[ip] = now + self.ban_seconds
                self._fails[ip] = []

    def record_ok(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)
            self._banned.pop(ip, None)
