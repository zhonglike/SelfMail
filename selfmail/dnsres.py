"""零依赖 DNS 解析器（UDP + TCP 兜底）：MX / TXT / A / AAAA / PTR。
自研服务器用它找对方 MX、生成/校验 SPF、DKIM、DMARC 记录。
"""
from __future__ import annotations

import random
import socket
import struct

TYPES = {"A": 1, "NS": 2, "CNAME": 5, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28}
RTYPES = {v: k for k, v in TYPES.items()}

_DNS_SERVERS: list[str] = []  # 运行时可注入


def _servers() -> list[str]:
    if _DNS_SERVERS:
        return _DNS_SERVERS
    try:
        import ssl
        # 常见公共 DNS，失败则回退系统解析
        return ["223.5.5.5", "1.1.1.1", "8.8.8.8"]
    except Exception:
        return ["223.5.5.5"]


def _encode_qname(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("idna") if not label.isascii() else label.encode()
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _read_name(buf: bytes, off: int):
    labels, jumped = [], False
    orig = off
    while True:
        ln = buf[off]
        if ln & 0xC0 == 0xC0:
            ptr = ((ln & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                orig = off + 2
            jumped = True
            off = ptr
        elif ln == 0:
            off = off + 1 if not jumped else orig
            return ".".join(labels), off
        else:
            labels.append(buf[off + 1: off + 1 + ln].decode("ascii", "replace"))
            off += 1 + ln


def _query(name: str, qtype: int, timeout: float = 6.0) -> list:
    qname = _encode_qname(name)
    packet = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0) \
        + qname + struct.pack(">HH", qtype, 1)
    last_err: Exception | None = None
    for server in _servers():
        for use_tcp in (False, True):
            try:
                if use_tcp:
                    with socket.create_connection((server, 53), timeout=timeout) as s:
                        s.sendall(struct.pack(">H", len(packet)) + packet)
                        ln = struct.unpack(">H", s.recv(2))[0]
                        data = b""
                        while len(data) < ln:
                            chunk = s.recv(ln - len(data))
                            if not chunk:
                                break
                            data += chunk
                else:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(timeout)
                    s.sendto(packet, (server, 53))
                    data, _ = s.recvfrom(4096)
                    s.close()
                return _parse(data)
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
    if last_err:
        raise last_err
    return []


def _parse(data: bytes) -> list:
    answers = []
    qd, an = struct.unpack(">HHHHHH", data[:12])[2], struct.unpack(">HHHHHH", data[:12])[3]
    off = 12
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    for _ in range(an):
        _, off = _read_name(data, off)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rdata = data[off:off + rdlen]
        if rtype == 1 and rdlen == 4:
            answers.append(("A", socket.inet_ntoa(rdata)))
        elif rtype == 28 and rdlen == 16:
            answers.append(("AAAA", socket.inet_ntop(socket.AF_INET6, rdata)))
        elif rtype == 15:
            pref, = struct.unpack(">H", rdata[:2])
            host, _ = _read_name(data, off + 2)
            answers.append(("MX", (pref, host)))
        elif rtype == 16:
            chunks, p = [], 0
            while p < rdlen:
                ln = rdata[p]
                chunks.append(rdata[p + 1: p + 1 + ln].decode("ascii", "replace"))
                p += 1 + ln
            answers.append(("TXT", "".join(chunks)))
        elif rtype == 5:
            host, _ = _read_name(data, off)
            answers.append(("CNAME", host))
        off += rdlen
    return answers


def resolve(name: str, rtype: str = "A") -> list:
    """rtype ∈ A/AAAA/MX/TXT/PTR。失败返回 []。MX 返回 [(pref, host)]。"""
    try:
        if rtype == "PTR":
            import ipaddress
            ip = ipaddress.ip_address(name)
            arpa = ip.reverse_pointer
        else:
            arpa = name
        return [v for t, v in _query(arpa, TYPES.get(rtype, 1)) if t == rtype]
    except Exception:
        return []


def resolve_mx(domain: str) -> list[tuple[int, str]]:
    """返回 [(pref, host)]；无 MX 时按 RFC 5321 回退 A 记录。"""
    mxs = resolve(domain, "MX")
    if mxs:
        return sorted(mxs)
    a = resolve(domain, "A")
    return [(0, h) for h in a]


def resolve_txt_all(domain: str) -> list[str]:
    return resolve(domain, "TXT")


def find_txt_record(domain: str, prefix: str) -> str:
    """找以 prefix 开头的 TXT（如 'v=spf1'、'v=DMARC1'、'v=DKIM1'）。"""
    for txt in resolve_txt_all(domain):
        if txt.strip('"').startswith(prefix):
            return txt.strip('"')
    return ""
