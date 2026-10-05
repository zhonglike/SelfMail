"""全局应用上下文：配置、存储、限流器、TLS 上下文、DKIM 密钥、唤醒事件。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .auth import RateLimiter
from .config import Config
from .store import Store


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(Path(cfg.data_dir))
        self.store.domains = cfg.domains          # 供 is_local_domain 使用
        self.limiter = RateLimiter(cfg.security["max_auth_fails"],
                                   cfg.security["ban_minutes"])
        self.wakeup = threading.Event()           # 出站队列立即投递信号
        self.started_at = time.time()
        self.tls_ctx = None                       # serve() 注入
        self._dkim_pem: bytes | None = None

    @property
    def dkim_pem(self) -> bytes | None:
        if self._dkim_pem is None:
            p = self.cfg.dkim_private_path()
            if p.exists():
                self._dkim_pem = p.read_bytes()
        return self._dkim_pem

    def reload_dkim(self):
        self._dkim_pem = None
        return self.dkim_pem
