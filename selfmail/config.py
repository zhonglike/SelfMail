"""配置加载与默认值。配置文件 data/config.json，首次运行自动生成。
所有路径相对项目根，可用绝对路径覆盖。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "domain": "example.com",            # 邮件域名（用户名@该域名）
    "extra_domains": [],                # 其他收信域名（可选）
    "hostname": "mail.example.com",     # 对外主机名（需与 TLS 证书 / DNS A 记录一致）
    "data_dir": "data",                 # 相对项目根；可用绝对路径
    "smtp": {
        "enable_in": True,              # 对外 MX 收信端口 25（家宽被封时设 false）
        "port_in": 25,
        "port_tls": 465,                # 隐式 TLS（客户端发信）
        "port_submit": 587,             # STARTTLS 提交
        "max_size_mb": 50,
        "relay_allow_ips": [],          # 允许免认证转发的外部 IP（一般留空）
    },
    "imap": {"port": 143, "port_tls": 993, "enable": True},
    "pop3": {"port": 110, "port_tls": 995, "enable": True},
    "web": {"port": 443, "enable_admin": True},
    "tls": {"cert": "data/certs/cert.pem", "key": "data/certs/key.pem"},
    "outbound": {
        "mode": "direct",               # direct=按 MX 直投；relay=经中继（家宽 25 被封时用）；disabled=仅排队
        "relay_host": "",               # relay 模式：中继主机（如 VPS 或邮件中继服务）
        "relay_port": 587,
        "relay_tls": "starttls",        # starttls | ssl | none
        "relay_user": "",
        "relay_pass": "",
        "max_attempts": 6,
        "tls_verify": True              # 出站 STARTTLS 证书校验（内网中继可关闭）
    },
    "dkim": {"enabled": True, "selector": "s1"},
    "security": {
        "require_tls_for_auth": True,   # 明文口令必须走 TLS
        "max_auth_fails": 5,
        "ban_minutes": 30,
        "allow_plain_imap_pop": False   # 是否允许 143/110 明文口令（纯内网调试才开）
    }
}


class Config:
    def __init__(self, data: dict, path: Path):
        self._d = data
        self.path = path
        root = self.path.parent.parent          # data/config.json → 项目根
        def p(v): return Path(v) if Path(v).is_absolute() else root / v
        self.root = root
        self.data_dir = p(self._d["data_dir"])
        self.cert_path = p(self._d["tls"]["cert"])
        self.key_path = p(self._d["tls"]["key"])
        self.dkim_dir = self.data_dir / "dkim"

    # ---- 便捷属性 ----
    @property
    def domain(self): return self._d["domain"]

    @property
    def hostname(self): return self._d["hostname"]

    @property
    def domains(self) -> set[str]:
        s = {self._d["domain"].lower(), self._d["hostname"].lower()}
        s |= {d.lower() for d in self._d.get("extra_domains", [])}
        return s

    @property
    def smtp(self): return self._d["smtp"]

    @property
    def imap(self): return self._d["imap"]

    @property
    def pop3(self): return self._d["pop3"]

    @property
    def web(self): return self._d["web"]

    @property
    def outbound(self): return self._d["outbound"]

    @property
    def dkim(self): return self._d["dkim"]

    @property
    def dkim_selector(self): return self._d["dkim"]["selector"]

    @property
    def security(self): return self._d["security"]

    @property
    def max_size_bytes(self): return int(self._d["smtp"]["max_size_mb"]) * 1024 * 1024

    def dkim_private_path(self) -> Path:
        return self.data_dir / "dkim" / f"{self.dkim_selector}.private.pem"

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._d, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | os.PathLike | None = None) -> Config:
    """加载配置；不存在则写默认。可用环境变量 SELFMAIL_CONFIG 指定路径。"""
    p = Path(path or os.environ.get("SELFMAIL_CONFIG") or (APP_ROOT / "data" / "config.json"))
    if p.exists():
        return Config(_merge(DEFAULTS, json.loads(p.read_text(encoding="utf-8"))), p)
    cfg = Config(json.loads(json.dumps(DEFAULTS)), p)
    cfg.save()
    return cfg
