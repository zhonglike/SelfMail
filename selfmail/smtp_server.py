"""SMTP 服务器（线程版）：25 MX 收信 / 465 隐式 TLS / 587 STARTTLS 提交。
安全策略：465/587 必须认证；25 端口只收本域收件人（防开放中继）；
出站邮件经 DKIM 签名后进入队列，由出站线程投递（直连 MX 或 smarthost 中继）。
"""
from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import re
import smtplib
import socket
import ssl
import time
from pathlib import Path

from .netutil import LineConn, serve_forever
from .store import parse_message


def _addr_of(line: str) -> str:
    """从 MAIL FROM:/RCPT TO: 参数中取地址。"""
    m = re.search(r"<([^>]*)>", line)
    if m:
        return m.group(1).strip()
    return line.split(":", 1)[-1].split()[0].strip() if line.split(":", 1)[-1].split() else ""


class SMTPSession:
    def __init__(self, app, conn: LineConn):
        self.app = app
        self.cfg = app.cfg
        self.store = app.store
        self.conn = conn
        self.user: str | None = None
        self.tls = conn.tls
        self.helo = ""
        self.env_from = ""
        self.rcpts: list[tuple[str, str]] = []  # (rcpt, target|None)
        self.secure = conn.tls

    def send(self, s: str):
        self.conn.send(s)

    def run(self):
        ip = self.conn.addr[0] if isinstance(self.conn.addr, tuple) else str(self.conn.addr)
        self.ip = ip
        try:
            self.send(f"220 {self.cfg.hostname} ESMTP SelfMail ready")
            while True:
                line = self.conn.read_line()
                if line is None:
                    return
                verb = line.split(":", 1)[0].split(" ")[0].strip().upper() if line.strip() else ""
                arg = line[len(verb):].strip() if verb else ""
                fn = getattr(self, f"cmd_{verb}", None) if verb and verb.isalnum() else None
                if fn:
                    if fn(arg) is False:
                        return
                else:
                    self.send("500 5.5.2 unknown command")
        finally:
            pass

    # ---------- 命令 ----------
    def cmd_EHLO(self, arg: str):
        self.helo = arg.split()[0] if arg.split() else ""
        exts = [f"250-{self.cfg.hostname}", f"250-SIZE {self.cfg.max_size_bytes}",
                "250-8BITMIME", "250-PIPELINING"]
        if not self.tls and self.cfg.smtp["port_submit"] and not self._is_465():
            exts.append("250-STARTTLS")
        if self._is_submit():
            exts.append("250-AUTH PLAIN LOGIN")
        exts.append("250 HELP")
        for e in exts:
            self.send(e)
        return True

    def _is_465(self) -> bool:
        return self.conn.tls

    def _is_submit(self) -> bool:
        """465/587 为提交端口（需认证）；25 为 MX 收信端口。"""
        return not getattr(self, "mx", False)

    def cmd_HELO(self, arg: str):
        self.helo = arg.split()[0] if arg.split() else ""
        self.send(f"250 {self.cfg.hostname}")
        return True

    def cmd_STARTTLS(self, arg: str):
        if self.tls:
            self.send("454 5.5.1 TLS already active")
            return True
        self.send("220 2.0.0 Ready to start TLS")
        if not self.conn.starttls(self.app.tls_ctx):
            return False
        self.tls = True
        self.secure = True
        self.helo = ""
        return True

    def cmd_AUTH(self, arg: str):
        if self.user:
            self.send("503 5.5.1 already authenticated")
            return True
        if not self.tls and self.cfg.security.get("require_tls_for_auth", True):
            self.send("538 5.7.11 encryption required (STARTTLS or port 465)")
            return True
        if self.app.limiter.is_banned(self.ip):
            self.send("554 5.7.1 too many failures, try later")
            return True
        parts = arg.split(None, 1)
        mech = parts[0].upper() if parts else ""
        user = pw = ""
        try:
            if mech == "PLAIN":
                blob = parts[1] if len(parts) > 1 else ""
                if not blob:
                    self.send("334 ")
                    blob = self.conn.read_line() or ""
                if blob == "*":
                    self.send("501 5.7.0 cancelled")
                    return True
                fields = base64.b64decode(blob).decode("utf-8", "replace").split("\x00")
                user = fields[-2] if len(fields) >= 2 else ""
                pw = fields[-1] if fields else ""
            elif mech == "LOGIN":
                u = parts[1] if len(parts) > 1 else None
                if u is None:
                    self.send("334 VXNlcm5hbWU6")
                    u = self.conn.read_line() or ""
                if u == "*":
                    self.send("501 5.7.0 cancelled")
                    return True
                user = base64.b64decode(u).decode("utf-8", "replace")
                self.send("334 UGFzc3dvcmQ6")
                p = self.conn.read_line() or ""
                if p == "*":
                    self.send("501 5.7.0 cancelled")
                    return True
                pw = base64.b64decode(p).decode("utf-8", "replace")
            else:
                self.send("504 5.5.4 mechanism not supported")
                return True
        except Exception:  # noqa: BLE001
            self.send("535 5.7.8 malformed auth")
            return True
        name = user.strip().lower().split("@")[0]
        if self.store.verify_login(name, pw):
            self.app.limiter.record_ok(self.ip)
            self.user = name
            self.store.log("info", "smtp_auth", name, self.ip)
            self.send("235 2.7.0 authentication successful")
        else:
            self.app.limiter.record_fail(self.ip)
            self.store.log("warn", "smtp_auth_fail", name, self.ip)
            self.send("535 5.7.8 authentication failed")
        return True

    def cmd_MAIL(self, arg: str):
        if not self.helo:
            self.send("503 5.5.1 send HELO/EHLO first")
            return True
        if self.env_from:
            self.send("503 5.5.1 nested MAIL")
            return True
        if not self.user and self._is_submit() and not self._relay_allowed():
            # 提交端口(465/587)必须先认证；MX 端口(25)允许匿名投递本域
            self.send("530 5.7.0 authentication required")
            return True
        addr = _addr_of(arg)
        m = re.search(r"SIZE=(\d+)", arg.upper())
        if m and int(m.group(1)) > self.cfg.max_size_bytes:
            self.send("552 5.3.4 message too large")
            return True
        self.env_from = addr
        self.rcpts = []
        self.send("250 2.1.0 originator ok")
        return True

    def _relay_allowed(self) -> bool:
        return self.ip in self.cfg.smtp.get("relay_allow_ips", [])

    def _is_mx_port(self) -> bool:
        return getattr(self, "port", None) == int(self.cfg.smtp.get("port_in", 25))

    def cmd_RCPT(self, arg: str):
        if not self.env_from:
            self.send("503 5.5.1 need MAIL first")
            return True
        addr = _addr_of(arg).lower()
        if not addr:
            self.send("501 5.1.3 bad address")
            return True
        domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        if self.store.is_local_domain(domain):
            target = self.store.resolve_rcpt(addr)
            if not target:
                self.send("550 5.1.1 no such user")
                return True
            self.rcpts.append((addr, target))
            self.send("250 2.1.5 recipient ok")
            return True
        if self.user or self._relay_allowed():
            self.rcpts.append((addr, None))
            self.send("250 2.1.5 recipient ok (relay)")
            return True
        self.send("550 5.7.1 relay denied")
        self.store.log("warn", "relay_denied", addr, self.ip)
        return True

    def cmd_DATA(self, arg: str):
        if not self.rcpts:
            self.send("503 5.5.1 need RCPT first")
            return True
        self.send("354 end data with <CRLF>.<CRLF>")
        buf = bytearray()
        too_big = False
        while True:
            line = self.conn.read_line(limit=65536)
            if line is None:
                return False
            if line == ".":
                break
            if line.startswith(".."):
                line = line[1:]          # 去点填充
            raw = (line + "\r\n").encode("utf-8", "surrogateescape")
            if len(buf) + len(raw) > self.cfg.max_size_bytes:
                too_big = True
                continue
            buf.extend(raw)
        if too_big:
            self._reset()
            self.send("552 5.3.4 message too large")
            return True
        try:
            self._finalize(bytes(buf))
        except Exception as e:  # noqa: BLE001
            self.store.log("error", "smtp_data_err", repr(e), self.ip)
            self.send("451 4.3.0 internal error")
            return True
        self._reset()
        return True

    def _reset(self):
        self.env_from = ""
        self.rcpts = []

    def cmd_RSET(self, arg: str):
        self._reset()
        self.send("250 2.0.0 ok")
        return True

    def cmd_NOOP(self, arg: str):
        self.send("250 2.0.0 ok")
        return True

    def cmd_QUIT(self, arg: str):
        self.send(f"221 2.0.0 {self.cfg.hostname} closing")
        return False

    def cmd_VRFY(self, arg: str):
        self.send("252 2.5.2 cannot verify")
        return True

    # ---------- 投递 ----------
    def _finalize(self, raw: bytes):
        raw = _prepend_received(raw, self)
        if not self.env_from:
            self.env_from = "unknown@unknown"
        local, remote = [], []
        for rcpt, target in self.rcpts:
            (local if target else remote).append((rcpt, target))

        # 本地投递
        for rcpt, target in local:
            _, hdrs, text, html, atts = parse_message(raw)
            self.store.deliver(target, "INBOX", raw, "", hdrs.get("From", self.env_from),
                               rcpt, hdrs.get("Subject", ""), hdrs.get("Date", ""),
                               text, bool(atts))

        # 出站：DKIM 签名一次，逐收件人入队
        if remote:
            if self.app.dkim_pem:
                from .dkim import sign as dkim_sign
                try:
                    raw = dkim_sign(raw, self.cfg.domain, self.cfg.dkim_selector,
                                    self.app.dkim_pem)
                except Exception:  # noqa: BLE001
                    pass
            for rcpt, _ in remote:
                self.store.enqueue(self.env_from, rcpt, raw)
            self.app.wakeup.set()
        n_local, n_remote = len(local), len(remote)
        self.store.log("info", "smtp_deliver",
                       f"{self.env_from} -> 本地{n_local}/出站{n_remote} from={self.ip}")
        self.send("250 2.0.0 accepted for delivery")


def _prepend_received(raw: bytes, sess: "SMTPSession") -> bytes:
    proto = "ESMTPS" if sess.secure else "ESMTP"
    ident = sess.user or getattr(sess, "helo", "") or "unknown"
    recv = (f"Received: from {ident} ([{sess.ip}])\r\n"
            f"\tby {sess.cfg.hostname} with {proto} id {email.utils.make_msgid(domain=sess.cfg.domain)[2:]};\r\n"
            f"\t{email.utils.formatdate(localtime=False)}\r\n").encode()
    return recv + raw


# ---------- 出站投递线程 ----------
def _mx_deliver(rcpt: str, raw: bytes, cfg, verify: bool) -> None:
    from . import dnsres
    domain = rcpt.rsplit("@", 1)[-1]
    mxs = dnsres.resolve_mx(domain)
    if not mxs:
        raise smtplib.SMTPConnectError(451, f"no MX for {domain}")
    last = None
    for pref, host in sorted(mxs)[:3]:
        try:
            with smtplib.SMTP(host, 25, timeout=30) as s:
                s.ehlo(cfg.hostname)
                if s.has_extn("starttls"):
                    s.starttls(context=ssl.create_default_context() if verify else
                               _no_verify_ctx())
                    s.ehlo(cfg.hostname)
                s.sendmail(cfg.hostname and cfg.domain and f"postmaster@{cfg.domain}",
                           [rcpt], raw)
                return
        except Exception as e:  # noqa: BLE001
            last = e
    raise last or smtplib.SMTPConnectError(451, "all MX failed")


def _no_verify_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _relay_deliver(rcpt: str, raw: bytes, cfg) -> None:
    o = cfg.outbound
    with smtplib.SMTP(o["relay_host"], int(o.get("relay_port") or 587), timeout=30) as s:
        s.ehlo(cfg.hostname)
        if o.get("relay_tls", "starttls") == "starttls" and s.has_extn("starttls"):
            s.starttls(context=_no_verify_ctx())
            s.ehlo(cfg.hostname)
        if o.get("relay_user"):
            s.login(o["relay_user"], o["relay_pass"])
        s.sendmail(cfg.hostname and f"postmaster@{cfg.domain}", [rcpt], raw)


def _bounce(app, sender: str, rcpt: str, err: str):
    if not sender or "@" not in sender:
        return
    if app.store.resolve_rcpt(sender):
        body = (f"From: MAILER-DAEMON@{app.cfg.hostname}\r\n"
                f"To: {sender}\r\n"
                f"Subject: Undelivered Mail returned to sender\r\n"
                f"Date: {email.utils.formatdate(localtime=False)}\r\n"
                f"Message-ID: {email.utils.make_msgid(domain=app.cfg.domain)}\r\n"
                f"\r\n"
                f"投递失败：{rcpt}\r\n\r\n错误信息：{err}\r\n\r\n"
                f"-- SelfMail 出站队列\r\n").encode()
        app.store.deliver(app.store.resolve_rcpt(sender), "INBOX", body, "",
                          f"MAILER-DAEMON@{app.cfg.hostname}", sender,
                          "投递失败通知", email.utils.formatdate(), f"投递失败：{rcpt} {err}")


def run_outbound_worker(app):
    """出站队列投递线程：等待唤醒或周期扫描，指数退避重试。"""
    cfg, store = app.cfg, app.store
    while True:
        app.wakeup.wait(timeout=20)
        app.wakeup.clear()
        try:
            if cfg.outbound["mode"] == "disabled":
                continue
            due = store.due_queue(50)
            for job in due:
                if cfg.outbound["mode"] == "relay":
                    fn = _relay_deliver
                    verify = True
                else:
                    fn = _mx_deliver
                    verify = bool(cfg.outbound.get("tls_verify", True))
                raw = (store.queue_dir / job["rawfile"]).read_bytes()
                try:
                    fn(job["rcpt"], raw, cfg, verify) if fn is _mx_deliver \
                        else fn(job["rcpt"], raw, cfg)
                except Exception as e:  # noqa: BLE001
                    status = store.queue_fail(job["id"], repr(e)[:400],
                                              cfg.outbound["max_attempts"])
                    if status == "bounced":
                        _bounce(app, job["sender"], job["rcpt"], repr(e)[:300])
                    continue
                store.queue_done(job["id"])
                store.log("info", "sent", f"-> {job['rcpt']}")
        except Exception as e:  # noqa: BLE001
            store.log("error", "outbound_worker", repr(e))


def start_listeners(app, tls_ctx):
    stops = []
    cfg = app.cfg
    s = cfg.smtp

    def on_conn(port: int, implicit_tls: bool):
        def handler(c: LineConn):
            sess = SMTPSession(app, c)
            sess.port = port
            sess.mx = (port == int(s.get("port_in", 25)))   # 25 = MX 收信端口
            if implicit_tls:
                sess.tls = sess.secure = True
            sess.run()
        return handler

    if s.get("port_tls", 465):
        stops.append(serve_forever("0.0.0.0", int(s["port_tls"]),
                                   on_conn(int(s["port_tls"]), True),
                                   "smtp-465", tls_ctx=tls_ctx, tls_only=True))
    if s.get("port_submit", 587):
        stops.append(serve_forever("0.0.0.0", int(s["port_submit"]),
                                   on_conn(int(s["port_submit"]), False),
                                   "smtp-587"))
    if s.get("enable_in", True) and s.get("port_in", 25):
        stops.append(serve_forever("0.0.0.0", int(s["port_in"]),
                                   on_conn(int(s["port_in"]), False),
                                   "smtp-25"))
    return stops
