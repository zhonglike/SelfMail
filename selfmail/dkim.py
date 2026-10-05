"""DKIM 签名器（rsa-sha256, relaxed/relaxed）—— 纯自实现，cryptography 仅做 RSA 私钥运算。
密钥由 cli gen-dns / gen-cert 生成：data/dkim/<selector>.private.pem
"""
from __future__ import annotations

import base64
import hashlib
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

_SIGN_HEADERS = ["From", "To", "Subject", "Date", "Message-ID", "Reply-To", "Cc"]


def _canon_header_value(name: str, value: str) -> str:
    v = value.replace("\r\n", "").replace("\n", "")
    v = " ".join(v.split())  # 折叠 + 压缩 WSP
    return f"{name.lower().strip()}:{v.strip()}"


def _canon_body(body: bytes) -> bytes:
    text = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    text = b"\n".join(line.rstrip(b" \t") for line in text.split(b"\n"))
    text = text.rstrip(b"\n") + b"\r\n"
    if text == b"\r\n":
        return b""
    return text


def generate_keypair() -> tuple[bytes, str]:
    """返回 (private_pem, dns_public_txt)。"""
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    pub_b64 = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    return priv_pem, pub_b64


def sign(raw: bytes, domain: str, selector: str, private_pem: bytes) -> bytes:
    """对完整原始邮件(raw)做 DKIM 签名，返回加了 DKIM-Signature 头的新邮件。"""
    if b"\r\n\r\n" in raw:
        head, body = raw.split(b"\r\n\r\n", 1)
    else:
        head, body = raw, b""
    headers: list[tuple[str, str]] = []
    for line in head.split(b"\r\n"):
        if line[:1] in (b" ", b"\t") and headers:
            headers[-1] = (headers[-1][0], headers[-1][1] + line.decode("utf-8", "replace"))
        elif b":" in line:
            k, _, v = line.decode("utf-8", "replace").partition(":")
            headers.append((k.strip(), v))
    present = [k for k in _SIGN_HEADERS if any(k.lower() == h.lower() for h, _ in headers)]
    canon_headers = [_canon_header_value(k, v) for k, v in headers if k.lower() in {p.lower() for p in present}]

    bh = base64.b64encode(hashlib.sha256(_canon_body(body)).digest()).decode()
    h_list = ":".join(present)
    dkim_base = (f"v=1; a=rsa-sha256; c=relaxed/relaxed; d={domain}; s={selector}; "
                 f"t={int(time.time())}; bh={bh}; h={h_list}; b=")
    data = (b"".join((h + "\r\n").encode() for h in canon_headers)
            + b"dkim-signature:" + dkim_base.encode())

    key = serialization.load_pem_private_key(private_pem, password=None)
    sig = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    b64 = base64.b64encode(sig).decode()
    # 每 70 字符折叠，符合头部规范
    folded = "\r\n\t".join(b64[i:i + 70] for i in range(0, len(b64), 70))
    return b"DKIM-Signature: " + (dkim_base + folded).encode() + b"\r\n" + raw


def ensure_keys(cfg) -> str:
    """确保 DKIM 密钥存在，返回 DNS TXT 值（p= 后的公钥）。"""
    from pathlib import Path
    kdir = Path(cfg.dkim_dir)
    kdir.mkdir(parents=True, exist_ok=True)
    priv_path = kdir / f"{cfg.dkim_selector}.private.pem"
    if not priv_path.exists():
        priv_pem, pub_txt = generate_keypair()
        priv_path.write_bytes(priv_pem)
        (kdir / f"{cfg.dkim_selector}.public.txt").write_text(pub_txt)
    else:
        pub_txt = (kdir / f"{cfg.dkim_selector}.public.txt").read_text().strip()
    return pub_txt
