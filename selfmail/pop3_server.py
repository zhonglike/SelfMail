"""POP3 服务端（995 隐式 TLS / 110 可选 STLS）。
USER/PASS、AUTH PLAIN、CAPA、STAT、LIST、UIDL、RETR、TOP、DELE、RSET、NOOP、QUIT。
邮箱 = INBOX；退出时应用删除标记。
"""
from __future__ import annotations

import base64

from .netutil import LineConn, serve_forever


class POP3Session:
    def __init__(self, app, conn: LineConn):
        self.app = app
        self.cfg = app.cfg
        self.store = app.store
        self.conn = conn
        self.user: str | None = None
        self.rows: list = []
        self.deleted: set[int] = set()

    def send(self, line: str):
        self.conn.send(line)

    def _load(self):
        self.rows = self.store._q(
            "SELECT * FROM messages WHERE user=? AND folder='INBOX' ORDER BY uid",
            (self.user,)).fetchall()
        self.deleted = set()

    def run(self):
        self.send(f"+OK SelfMail POP3 ready <{id(self):x}@{self.cfg.hostname}>")
        while True:
            line = self.conn.read_line(4096)
            if line is None:
                return
            parts = line.split(" ", 1)
            verb = parts[0].upper() if parts else ""
            arg = parts[1].strip() if len(parts) > 1 else ""
            try:
                if verb == "CAPA":
                    caps = ["IMPLEMENTATION SelfMail", "TOP", "UIDL", "USER", "PIPELINING"]
                    if not self.conn.tls:
                        caps.append("STLS")
                    self.send("+OK capability list follows")
                    for c in caps:
                        self.send(c)
                    self.send(".")
                elif verb == "STLS":
                    if self.conn.tls:
                        self.send("-ERR already in TLS")
                        continue
                    if not self.conn.starttls(self.app.tls_ctx):
                        return
                    self.send("+OK TLS started")
                elif verb == "USER":
                    self.user = arg.lower().strip()
                    self.send("+OK user accepted")
                elif verb == "PASS":
                    self._pass(arg)
                elif verb == "AUTH":
                    self._auth(arg)
                elif verb == "STAT":
                    self._stat()
                elif verb == "LIST":
                    self._list(arg)
                elif verb == "UIDL":
                    self._uidl(arg)
                elif verb == "RETR":
                    self._retr(arg)
                elif verb == "TOP":
                    self._top(arg)
                elif verb == "DELE":
                    self._dele(arg)
                elif verb == "RSET":
                    self.deleted = set()
                    self.send("+OK")
                elif verb == "NOOP":
                    self.send("+OK")
                elif verb == "QUIT":
                    self._quit()
                    return
                else:
                    self.send("-ERR unknown command")
            except (ConnectionError, TimeoutError, OSError):
                return
            except Exception as e:  # noqa: BLE001
                self.store.log("error", "pop3_cmd", f"{verb}: {e!r}")
                self.send("-ERR internal error")

    def _pass(self, pwd):
        if not self.user:
            self.send("-ERR USER first")
            return
        ip = self.conn.peer_ip()
        if self.app.limiter.is_banned(ip):
            self.send("-ERR too many failures")
            return
        if not (self.conn.tls or self.cfg.security["allow_plain_imap_pop"]):
            self.send("-ERR TLS required (use 995 or STLS)")
            return
        if self.store.verify_login(self.user, pwd):
            self.app.limiter.record_ok(ip)
            self.store.log("info", "pop3_auth", self.user, ip)
            self._load()
            self.send(f"+OK mailbox open, {len(self.rows)} messages")
        else:
            self.app.limiter.record_fail(ip)
            self.store.log("warn", "pop3_auth_fail", self.user, ip)
            self.send("-ERR invalid credentials")

    def _auth(self, arg):
        mech = arg.split(" ")[0].upper() if arg else ""
        if mech == "PLAIN":
            b64 = arg.split(" ", 1)[1] if " " in arg else None
            if not b64:
                self.send("+ ")
                b64 = self.conn.read_line(8192)
            try:
                p = base64.b64decode((b64 or "").strip()).split(b"\x00")
                self.user = p[1].decode("utf-8", "replace").lower()
                self._pass(p[2].decode("utf-8", "replace"))
            except Exception:
                self.send("-ERR bad AUTH")
        else:
            self.send("-ERR only PLAIN supported")

    def _alive_rows(self):
        return [r for i, r in enumerate(self.rows, 1) if i not in self.deleted]

    def _stat(self):
        rows = self._alive_rows()
        self.send(f"+OK {len(rows)} {sum(r['size'] for r in rows)}")

    def _list(self, arg):
        rows = self._alive_rows()
        if arg:
            n = int(arg)
            if 1 <= n <= len(rows):
                self.send(f"+OK {n} {rows[n - 1]['size']}")
            else:
                self.send("-ERR no such message")
            return
        self.send(f"+OK {len(rows)} messages")
        for i, r in enumerate(rows, 1):
            self.send(f"{i} {r['size']}")
        self.send(".")

    def _uidl(self, arg):
        rows = self._alive_rows()
        if arg:
            n = int(arg)
            if 1 <= n <= len(rows):
                self.send(f"+OK {n} {rows[n - 1]['uid']:016x}")
            else:
                self.send("-ERR no such message")
            return
        self.send("+OK unique-id listing follows")
        for i, r in enumerate(rows, 1):
            self.send(f"{i} {r['uid']:016x}")
        self.send(".")

    def _retr(self, arg):
        rows = self._alive_rows()
        try:
            n = int(arg)
        except ValueError:
            self.send("-ERR bad number")
            return
        if not (1 <= n <= len(rows)):
            self.send("-ERR no such message")
            return
        row = rows[n - 1]
        raw = self.store.read_raw(row)
        if "\\Seen" not in row["flags"]:
            self.store.set_flags(self.user, row["id"], row["flags"] + " \\Seen")
        self.send(f"+OK {len(raw)} octets")
        self._send_raw(raw)

    def _top(self, arg):
        rows = self._alive_rows()
        try:
            n, k = (int(x) for x in arg.split()[:2])
        except ValueError:
            self.send("-ERR bad arguments")
            return
        if not (1 <= n <= len(rows)):
            self.send("-ERR no such message")
            return
        raw = self.store.read_raw(rows[n - 1])
        head, sep, body = raw.partition(b"\r\n\r\n")
        self.send("+OK top of message follows")
        lines = body.split(b"\r\n")[:k] if sep else []
        self._send_raw(head + (sep + b"\r\n".join(lines) if sep else b""))

    def _send_raw(self, raw: bytes):
        # 字节填充：行首 . → ..
        for ln in raw.split(b"\r\n"):
            self.conn.send_raw(b"." + ln if ln.startswith(b".") else ln)
            self.conn.send_raw(b"\r\n")
        self.conn.send_raw(b".\r\n")

    def _dele(self, arg):
        rows = self._alive_rows()
        try:
            n = int(arg)
        except ValueError:
            self.send("-ERR bad number")
            return
        if not (1 <= n <= len(rows)):
            self.send("-ERR no such message")
            return
        self.deleted.add(n)
        self.send("+OK marked deleted")

    def _quit(self):
        if self.user:
            rows = self._alive_rows()
            keep_ids = {r["id"] for r in rows}
            for i, r in enumerate(self.rows, 1):
                if r["id"] not in keep_ids:
                    self.store.expunge_id(self.user, r["id"])
            self.send("+OK SelfMail signing off")


def start_listeners(app, tls_ctx):
    stops = []
    cfg = app.cfg
    if cfg.pop3.get("enable", True):
        if cfg.pop3.get("port_tls", 995):
            stops.append(serve_forever("0.0.0.0", int(cfg.pop3["port_tls"]),
                                       lambda c: POP3Session(app, c).run(),
                                       "pop3-995", tls_ctx=tls_ctx, tls_only=True))
        if cfg.pop3.get("port", 110):
            stops.append(serve_forever("0.0.0.0", int(cfg.pop3["port"]),
                                       lambda c: POP3Session(app, c).run(),
                                       "pop3-110"))
    return stops
