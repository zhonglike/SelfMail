"""DNS 记录生成与在线核查（MX / SPF / DKIM / DMARC / 自动发现 / 25 端口探测）。"""
from __future__ import annotations

import json
import socket
import textwrap
import urllib.request

from . import dnsres
from .dkim import ensure_keys

_IP_CACHE: str | None = None


def detect_public_ip(timeout: float = 6.0) -> str:
    """自动检测本机公网 IPv4，多来源容错；失败返回占位符。"""
    global _IP_CACHE
    if _IP_CACHE:
        return _IP_CACHE
    sources = [
        ("https://api.ipify.org?format=json", lambda raw: json.loads(raw).get("ip", "")),
        ("https://ipv4.icanhazip.com", str.strip),
        ("https://ifconfig.me/ip", str.strip),
    ]
    for url, parse in sources:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                ip = parse(resp.read(256).decode("utf-8", "replace")).strip()
            if ip and ip.count(".") == 3 and all(p.isdigit() for p in ip.split(".")):
                _IP_CACHE = ip
                return ip
        except Exception:
            continue
    return "<你的公网IP>"


def records(cfg) -> list[dict]:
    sel = cfg.dkim["selector"]
    dkim_txt = ""
    pub = cfg.dkim_dir / f"{sel}.public.txt"
    if pub.exists():
        p = pub.read_text(encoding="utf-8").strip()
        dkim_txt = f"v=DKIM1; k=rsa; {p}"
    elif cfg.dkim.get("enabled", True):
        try:
            ensure_keys(cfg)
            p = (cfg.dkim_dir / f"{sel}.public.txt").read_text(encoding="utf-8").strip()
            dkim_txt = f"v=DKIM1; k=rsa; {p}"
        except Exception:
            pass
    d, h = cfg.domain, cfg.hostname
    ip = detect_public_ip()
    recs = [
        {"type": "A", "name": h, "value": ip,
         "note": f"自动检测到的出口 IP：{ip}；动态 IP 请用 DDNS 保持更新"},
        {"type": "MX", "name": d, "value": f"10 {h}",
         "note": "把 @域名 的邮件路由到本服务器"},
        {"type": "TXT", "name": d, "value": "v=spf1 mx a -all",
         "note": "SPF：仅允许本服务器代发"},
        {"type": "TXT", "name": f"_dmarc.{d}",
         "value": "v=DMARC1; p=quarantine; rua=mailto:postmaster@" + d,
         "note": "DMARC 策略；验证通过后可升为 p=reject"},
        {"type": "CNAME", "name": f"autoconfig.{d}", "value": h,
         "note": "Thunderbird 自动发现配置"},
        {"type": "CNAME", "name": f"autodiscover.{d}", "value": h,
         "note": "Outlook 自动发现"},
    ]
    if dkim_txt:
        recs.append({"type": "TXT", "name": f"{sel}._domainkey.{d}",
                     "value": dkim_txt, "note": "DKIM 公钥（发信签名验证）"})
    return recs


def check(cfg) -> list[dict]:
    out = []
    d, h = cfg.domain, cfg.hostname
    sel = cfg.dkim["selector"]

    def add(name, expected, found):
        found = found or []
        ok = any(expected.split()[0] in str(f) or expected in str(f) for f in found) \
            if expected else bool(found)
        out.append({"record": name, "expected": expected,
                    "found": "; ".join(found)[:400] or "（未查到）", "ok": ok})

    mx = dnsres.resolve_mx(d)
    add(f"MX {d}", h, [f"{p} {v}" for p, v in mx])

    a = dnsres.resolve(h)
    add(f"A {h}", "", a)

    spf = dnsres.find_txt_record(d, "v=spf1")
    add(f"TXT SPF {d}", "v=spf1", [spf] if spf else [])

    dk = dnsres.find_txt_record(f"{sel}._domainkey.{d}", "v=DKIM1")
    add(f"TXT DKIM {sel}._domainkey.{d}", "v=DKIM1", [dk] if dk else [])

    dm = dnsres.find_txt_record(f"_dmarc.{d}", "v=DMARC1")
    add(f"TXT DMARC _dmarc.{d}", "v=DMARC1", [dm] if dm else [])

    # 出站 25 端口探测（家宽普遍封锁）
    try:
        s = socket.create_connection(("mx1.qq.com", 25), timeout=6)
        s.close()
        out.append({"record": "出站端口 25", "expected": "可连通",
                    "found": "可连通（可直接对外投递）", "ok": True})
    except OSError as e:
        out.append({"record": "出站端口 25", "expected": "可连通",
                    "found": f"被封锁（{e}）→ 请在 config.json 将 outbound.mode 改为 relay",
                    "ok": False})
    return out


def format_records(recs: list[dict]) -> str:
    lines = ["# 在你的 DNS 服务商处添加以下记录（示例以 Cloudflare/阿里云等通用格式为准）", ""]
    for r in recs:
        lines.append(f"[{r['type']}]  {r['name']}")
        wrapped = textwrap.fill(r["value"], width=68, subsequent_indent="        ")
        lines.append("        " + wrapped[8:] if wrapped.startswith("        ") else wrapped)
        if r.get("note"):
            lines.append(f"        # {r['note']}")
        lines.append("")
    return "\n".join(lines)
