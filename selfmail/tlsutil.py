"""TLS 证书与 SSL 上下文：无证书时自动生成自签名（含 SAN）。
生产建议用 Let's Encrypt（win-acme/certbot）签发后在 config 中指向真实 pem。
"""
from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
from pathlib import Path


def generate_self_signed(cert_path: Path, key_path: Path, hostname: str, domain: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [hostname, domain, "localhost"]
    san = [x509.DNSName(n) for n in dict.fromkeys(names) if n]
    for ip in {socket.gethostbyname(socket.gethostname()), "127.0.0.1"}:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))


def ensure_cert(cfg) -> tuple[Path, Path]:
    cert_path = Path(cfg.cert_path)
    key_path = Path(cfg.key_path)
    if not (cert_path.exists() and key_path.exists()):
        generate_self_signed(cert_path, key_path, cfg.hostname, cfg.domain)
    return cert_path, key_path


def make_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def client_context(verify: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
