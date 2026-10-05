"""HTTPS Web 服务：Webmail/管理后台 REST API + 静态页 + 客户端自动配置。
复用 SMTP 层的出站/DKIM 逻辑；会话 Cookie + CSRF；仅 TLS。
"""
from __future__ import annotations

import email
import email.message
import email.policy
import email.utils
import html as html_mod
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from email.message import EmailMessage

from .store import DEFAULT_FOLDERS, parse_message

_SESSION_TTL = 7 * 24 * 3600
WEBUI = Path(__file__).parent / "webui"


class Sessions:
    def __init__(self):
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()

    def new(self, user: str) -> tuple[str, str]:
        tok = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._lock:
            self._d[tok] = {"user": user, "csrf": csrf, "exp": time.time() + _SESSION_TTL}
        return tok, csrf

    def get(self, tok: str) -> dict | None:
        with self._lock:
            s = self._d.get(tok)
            if s and s["exp"] > time.time():
                return s
            if s:
                self._d.pop(tok, None)
        return None

    def drop(self, tok: str):
        with self._lock:
            self._d.pop(tok, None)


def _sanitize_html(h: str) -> str:
    if not h:
        return ""
    h = re.sub(r"(?is)<(script|iframe|object|embed|form)[^>]*>.*?</\1>", "", h)
    h = re.sub(r"(?is)<(script|iframe|object|embed|form)[^>]*/?>", "", h)
    h = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    h = re.sub(r"(?i)javascript\s*:", "", h)
    return h


class WebHandler(BaseHTTPRequestHandler):
    server_version = "SelfMail"
    app = None          # 注入
    sessions = None

    # ---- 基础 ----
    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _json(self, obj, code=200, headers: dict | None = None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _cookie_tok(self) -> str:
        c = self.headers.get("Cookie", "")
        m = re.search(r"sm_session=([^;]+)", c)
        return m.group(1) if m else ""

    def _sess(self):
        return self.sessions.get(self._cookie_tok())

    def _read_body(self, limit: int) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("payload too large")
        return self.rfile.read(n)

    def _csrf_ok(self, sess) -> bool:
        if self.headers.get("X-CSRF") == sess["csrf"]:
            return True
        return False

    # ---- 入口 ----
    def do_GET(self):
        try:
            self._route("GET")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._err(500, str(e))

    def do_POST(self):
        try:
            self._route("POST")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._err(500, str(e))

    def do_DELETE(self):
        self.do_POST()

    def do_PATCH(self):
        self.do_POST()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET,POST,PATCH,DELETE,OPTIONS")
        self.end_headers()

    def _route(self, method):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api(method, path)
        if path in ("/mail/config-v1.1.xml",
                    "/.well-known/autoconfig/mail/config-v1.1.xml"):
            return self._autoconfig_tb()
        if path == "/Autodiscover/Autodiscover.xml" and method == "POST":
            return self._autodiscover_outlook()
        return self._static(path)

    # ---- 静态 ----
    def _static(self, path):
        if path in ("/", "/index.html", "/login", "/mail", "/admin"):
            fp = WEBUI / "index.html"
        else:
            fp = (WEBUI / path.lstrip("/")).resolve()
            if not str(fp).startswith(str(WEBUI.resolve())) or not fp.is_file():
                self._err(404, "not found")
                return
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8"
                         if ctype.startswith("text/") or ctype == "application/javascript"
                         else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- 自动配置 ----
    def _autoconfig_tb(self):
        cfg = self.app.cfg
        x = f"""<?xml version="1.0" encoding="UTF-8"?>
<clientConfig version="1.1">
 <emailProvider id="{cfg.domain}">
  <domain>{cfg.domain}</domain>
  <incomingServer type="imap"><hostname>{cfg.hostname}</hostname><port>993</port>
   <socketType>SSL</socketType><username>%EMAILADDRESS%</username>
   <authentication>password-cleartext</authentication></incomingServer>
  <outgoingServer type="smtp"><hostname>{cfg.hostname}</hostname><port>465</port>
   <socketType>SSL</socketType><username>%EMAILADDRESS%</username>
   <authentication>password-cleartext</authentication></outgoingServer>
 </emailProvider>
</clientConfig>"""
        data = x.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _autodiscover_outlook(self):
        cfg = self.app.cfg
        try:
            body = self._read_body(65536)
            m = re.search(rb"<EMailAddress>([^<]+)</EMailAddress>", body)
            addr = m.group(1).decode() if m else ""
        except Exception:
            addr = ""
        x = f"""<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
 <Response>
  <Account><AccountType>email</AccountType><Action>settings</Action>
   <Protocol><Type>IMAP</Type><Server>{cfg.hostname}</Server><Port>993</Port>
    <LoginName>{addr}</LoginName><SSL>on</SSL><AuthRequired>on</AuthRequired></Protocol>
   <Protocol><Type>SMTP</Type><Server>{cfg.hostname}</Server><Port>465</Port>
    <LoginName>{addr}</LoginName><SSL>on</SSL><AuthRequired>on</AuthRequired></Protocol>
  </Account>
 </Response>
</Autodiscover>"""
        data = x.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ================= REST API =================
    def _api(self, method, path):
        app, cfg, store = self.app, self.app.cfg, self.app.store
        sess = self._sess()
        ip = self.client_address[0]

        # ---- 公开：登录 ----
        if path == "/api/login" and method == "POST":
            if app.limiter.is_banned(ip):
                return self._err(429, "失败次数过多，请稍后再试")
            body = json.loads(self._read_body(1 << 20) or b"{}")
            name = (body.get("user") or "").strip().lower().split("@")[0]
            if not store.verify_login(name, body.get("pass") or ""):
                app.limiter.record_fail(ip)
                store.log("warn", "web_login_fail", name, ip)
                return self._err(401, "用户名或密码错误")
            app.limiter.record_ok(ip)
            tok, csrf = self.sessions.new(name)
            u = store.get_user(name)
            store.log("info", "web_login", name, ip)
            return self._json({"user": name, "display": u["display_name"],
                               "admin": bool(u["is_admin"]), "csrf": csrf},
                              headers={"Set-Cookie":
                                       f"sm_session={tok}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={_SESSION_TTL}"})

        if path == "/api/logout" and method == "POST":
            if sess:
                self.sessions.drop(self._cookie_tok())
            return self._json({"ok": True},
                              headers={"Set-Cookie": "sm_session=; Path=/; Max-Age=0"})

        # ---- 以下全部需要会话 ----
        if not sess:
            return self._err(401, "未登录")
        user = sess["user"]

        if method != "GET" and not self._csrf_ok(sess):
            return self._err(403, "CSRF 校验失败")

        if path == "/api/me" and method == "GET":
            u = store.get_user(user)
            return self._json({"user": user, "display": u["display_name"],
                               "admin": bool(u["is_admin"]), "csrf": sess["csrf"],
                               "quota_mb": u["quota_mb"], "used": u["used"],
                               "domain": cfg.domain})

        # ---- 文件夹 ----
        if path == "/api/folders" and method == "GET":
            out = []
            for r in store.list_folders(user):
                st = store.folder_status(user, r["name"])
                out.append({"name": r["name"], **(st or {})})
            return self._json(out)
        if path == "/api/folders" and method == "POST":
            body = json.loads(self._read_body(1 << 20) or b"{}")
            name = (body.get("name") or "").strip()
            if not name or "/" in name:
                return self._err(400, "文件夹名不合法")
            if body.get("new_name"):  # 重命名
                store.rename_folder(user, name, body["new_name"].strip())
                return self._json({"ok": True})
            return self._json({"ok": bool(store.ensure_folder(user, name))})
        m = re.fullmatch(r"/api/folders/(.+)", path)
        if m and method == "DELETE":
            name = urllib.parse.unquote(m.group(1))
            store.delete_folder(user, name)
            return self._json({"ok": True})

        # ---- 邮件列表 / 详情 ----
        if path == "/api/messages" and method == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            folder = (q.get("folder") or ["INBOX"])[0]
            text = (q.get("q") or [""])[0]
            unread = (q.get("unread") or ["0"])[0] == "1"
            rows = store.search_messages(user, folder, text, unread)
            rows = rows[-200:]
            return self._json([{"id": r["id"], "uid": r["uid"], "from": r["msg_from"],
                                "to": r["msg_to"], "subject": r["subject"], "date": r["date"],
                                "flags": r["flags"], "size": r["size"], "attach": bool(r["has_attach"])}
                               for r in rows])

        m = re.fullmatch(r"/api/message/(\d+)", path)
        if m and method == "GET":
            row = store.get_message(user, int(m.group(1)))
            if not row:
                return self._err(404, "邮件不存在")
            raw = store.read_raw(row)
            msg, hdrs, text, html, atts = parse_message(raw)
            cur_flags = row["flags"]
            if "\\Seen" not in cur_flags:
                f = set(cur_flags.split()) | {"\\Seen"}
                cur_flags = " ".join(sorted(f))
                store.set_flags(user, row["id"], cur_flags)
            return self._json({"id": row["id"], "folder": row["folder"], "flags": cur_flags,
                               "headers": hdrs, "text": text,
                               "html": _sanitize_html(html),
                               "attachments": [{"part": p, "name": n, "type": t, "size": len(b)}
                                               for p, n, t, b in atts]})

        m = re.fullmatch(r"/api/attachment/(\d+)/(.+)", path)
        if m and method == "GET":
            row = store.get_message(user, int(m.group(1)))
            if not row:
                return self._err(404, "邮件不存在")
            _, _, _, _, atts = parse_message(store.read_raw(row))
            want = urllib.parse.unquote(m.group(2))
            for p, name, ctype, data in atts:
                if p == want:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition",
                                     "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            return self._err(404, "附件不存在")

        # ---- 操作 ----
        if path == "/api/flags" and method == "POST":
            body = json.loads(self._read_body(1 << 20) or b"{}")
            row = store.get_message(user, int(body.get("id") or 0))
            if not row:
                return self._err(404, "邮件不存在")
            flags = set(row["flags"].split())
            flag = body.get("flag", "\\Seen")
            (flags.add if body.get("on") else flags.discard)(flag)
            store.set_flags(user, row["id"], " ".join(sorted(flags)))
            return self._json({"ok": True, "flags": " ".join(sorted(flags))})
        if path == "/api/move" and method == "POST":
            body = json.loads(self._read_body(1 << 20) or b"{}")
            dest = store.copy_message(user, int(body.get("id") or 0), body.get("to") or "Archive")
            if dest:
                store.expunge_id(user, int(body["id"]))
            return self._json({"ok": bool(dest)})
        if path == "/api/delete" and method == "POST":
            body = json.loads(self._read_body(1 << 20) or b"{}")
            return self._json({"ok": bool(store.delete_message(user, int(body.get("id") or 0)))})
        if path == "/api/password" and method == "POST":
            body = json.loads(self._read_body(1 << 20) or b"{}")
            if not store.verify_login(user, body.get("old") or ""):
                return self._err(401, "旧密码错误")
            if len(body.get("new") or "") < 8:
                return self._err(400, "新密码至少 8 位")
            store.set_password(user, body["new"])
            store.log("info", "web_pw_change", user, ip)
            return self._json({"ok": True})

        # ---- 发送 / 草稿 ----
        if path in ("/api/send", "/api/draft") and method == "POST":
            try:
                return self._send_or_draft(method, path)
            except ValueError as e:
                return self._err(400, str(e))

        # ---- 管理端 ----
        if path.startswith("/api/admin"):
            if not store.is_admin(user) or not cfg.web.get("enable_admin", True):
                return self._err(403, "需要管理员权限")
            return self._admin(method, path, ip)

        return self._err(404, "unknown api")

    # ---------- 发送 ----------
    def _send_or_draft(self, method, path):
        app, cfg, store = self.app, self.app.cfg, self.app.store
        user = self._sess()["user"]
        u = store.get_user(user)
        ctype = self.headers.get("Content-Type", "")
        body = self._read_body(cfg.max_size_bytes + (4 << 20))
        if "multipart/form-data" not in ctype:
            raise ValueError("需要 multipart/form-data")
        wrapper = email.message_from_bytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
        fields, files = {}, []
        for part in wrapper.get_payload():
            if part.is_multipart():
                continue
            name = part.get_param("name", header="content-disposition") or ""
            if part.get_filename():
                data = part.get_payload(decode=True) or b""
                files.append((part.get_filename(), data, part.get_content_type()))
            else:
                fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")

        msg = EmailMessage()
        msg["From"] = (f"{u['display_name']} <{user}@{cfg.domain}>"
                       if u["display_name"] else f"{user}@{cfg.domain}")
        to = [a.strip() for a in re.split(r"[,;，；]", fields.get("to", "")) if a.strip()]
        cc = [a.strip() for a in re.split(r"[,;，；]", fields.get("cc", "")) if a.strip()]
        bcc = [a.strip() for a in re.split(r"[,;，；]", fields.get("bcc", "")) if a.strip()]
        if to:
            msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = fields.get("subject", "")
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=cfg.domain)
        if fields.get("inreplyto"):
            msg["In-Reply-To"] = fields["inreplyto"]
            msg["References"] = fields["inreplyto"]
        msg.set_content(fields.get("body", ""))
        for fname, data, ftype in files:
            maintype, _, subtype = (ftype or "").partition("/")
            if not maintype:
                maintype, subtype = "application", "octet-stream"
            try:
                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
            except Exception:  # noqa: BLE001
                msg.add_attachment(data, maintype="application", subtype="octet-stream",
                                   filename=fname)
        raw = msg.as_bytes(policy=email.policy.SMTP)

        # DKIM 签名
        if app.dkim_pem:
            from .dkim import sign as dkim_sign
            try:
                raw = dkim_sign(raw, cfg.domain, cfg.dkim_selector, app.dkim_pem)
            except Exception:  # noqa: BLE001
                pass

        _, hdrs, text, html, atts = parse_message(raw)
        has_att = bool(atts)

        if path == "/api/draft":
            mid = store.deliver(user, "Drafts", raw, "\\Draft", str(msg["From"]),
                                ", ".join(to), fields.get("subject", ""), hdrs.get("Date", ""),
                                text, has_att)
            return self._json({"ok": True, "id": mid, "draft": True})

        if len(body) > cfg.max_size_bytes:
            raise ValueError("附件总体积超过上限")

        queued, local = 0, 0
        for rcpt in set(to + cc + bcc):
            if not rcpt:
                continue
            target = store.resolve_rcpt(rcpt)
            if target:
                store.deliver(target, "INBOX", raw, "", str(msg["From"]), rcpt,
                              fields.get("subject", ""), hdrs.get("Date", ""), text, has_att)
                local += 1
            elif rcpt.lower().endswith("@" + cfg.domain.lower()):
                continue  # 本域不存在账号：忽略
            else:
                store.enqueue(str(msg["From"]), rcpt, raw)
                queued += 1
        store.deliver(user, "Sent", raw, "\\Seen", str(msg["From"]), ", ".join(to + cc),
                      fields.get("subject", ""), hdrs.get("Date", ""), text, has_att)
        app.wakeup.set()
        store.log("info", "web_send", f"{user} 本地{local}/出站{queued}")
        return self._json({"ok": True, "queued": queued, "local": local})

    # ---------- 管理端 ----------
    def _admin(self, method, path, ip):
        app, cfg, store = self.app, self.app.cfg, self.app.store
        user = self._sess()["user"]

        def body_json():
            return json.loads(self._read_body(4 << 20) or b"{}")

        if path == "/api/admin/users" and method == "GET":
            return self._json([dict(r) for r in store.list_users()])
        if path == "/api/admin/users" and method == "POST":
            b = body_json()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", (b.get("user") or "").lower()):
                return self._err(400, "账号仅限 2-32 位小写字母/数字/._-")
            ok = store.create_user(b["user"].lower(), b.get("pass") or "", b.get("display", ""),
                                   bool(b.get("admin")), int(b.get("quota_mb") or 2048))
            store.log("info", "admin_user_add", b.get("user", ""), ip)
            return self._json({"ok": ok})
        m = re.fullmatch(r"/api/admin/users/(.+)", path)
        if m and method == "PATCH":
            b = body_json()
            fields = {}
            for k in ("quota_mb", "admin", "active"):
                if k in b:
                    fields[k] = b[k]
            if b.get("pass"):
                fields["password"] = b["pass"]
            store.update_user(m.group(1).lower(), **fields)
            store.log("info", "admin_user_edit", m.group(1), ip)
            return self._json({"ok": True})
        if m and method == "DELETE":
            target = m.group(1).lower()
            if target == user:
                return self._err(400, "不能删除自己")
            store.delete_user(target)
            store.log("warn", "admin_user_del", target, ip)
            return self._json({"ok": True})

        if path == "/api/admin/aliases" and method == "GET":
            return self._json([dict(r) for r in store.list_aliases()])
        if path == "/api/admin/aliases" and method == "POST":
            b = body_json()
            if b.get("delete"):
                store.del_alias(b["delete"].lower())
                return self._json({"ok": True})
            a = (b.get("alias") or "").lower()
            t = (b.get("target") or "").lower()
            if "@" not in a:
                a = f"{a}@{cfg.domain}"
            ok = store.add_alias(a, t)
            store.log("info", "admin_alias", f"{a} -> {t}", ip)
            return self._json({"ok": ok})

        if path == "/api/admin/queue" and method == "GET":
            return self._json([dict(r) for r in store.list_queue()])
        if path == "/api/admin/queue" and method == "POST":
            b = body_json()
            if b.get("clear"):
                store.queue_clear(b["clear"])
            elif b.get("retry"):
                store.queue_retry(b["retry"])
            self.app.wakeup.set()
            return self._json({"ok": True})

        if path == "/api/admin/logs" and method == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            rows = store.recent_logs(int((q.get("n") or ["300"])[0]), (q.get("level") or [""])[0])
            return self._json([dict(r) for r in rows])

        if path == "/api/admin/status" and method == "GET":
            import selfmail as _pkg
            data_dir = Path(cfg.data_dir)
            size = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file()) \
                if data_dir.exists() else 0
            return self._json({
                "version": _pkg.__version__,
                "domain": cfg.domain, "hostname": cfg.hostname,
                "users": len(store.list_users()),
                "queue": len(store.list_queue()),
                "uptime": time.time() - app.started_at,
                "outbound_mode": cfg.outbound["mode"],
                "data_size": size,
                "cert_exists": Path(cfg.cert_path).exists(),
                "dkim_selector": cfg.dkim_selector,
                "ports": {"smtp_in": cfg.smtp["port_in"] if cfg.smtp["enable_in"] else 0,
                          "smtp_tls": cfg.smtp["port_tls"], "smtp_submit": cfg.smtp["port_submit"],
                          "imap": cfg.imap["port"] if cfg.imap.get("enable", True) else 0,
                          "imap_tls": cfg.imap["port_tls"],
                          "pop3": cfg.pop3["port"] if cfg.pop3.get("enable", True) else 0,
                          "pop3_tls": cfg.pop3["port_tls"], "web": cfg.web["port"]},
                "lan_ip": _lan_ip()})

        if path == "/api/admin/dns" and method == "GET":
            from .dkim import ensure_keys
            pub = ensure_keys(cfg)
            d = cfg.domain
            sel = cfg.dkim_selector
            from .dnssetup import detect_public_ip
            return self._json({"records": [
                {"type": "A", "host": cfg.hostname, "value": detect_public_ip(),
                 "note": "服务器公网地址（已自动检测，DDNS 脚本可自动更新）"},
                {"type": "MX", "host": d, "value": f"10 {cfg.hostname}", "note": "收信路由"},
                {"type": "TXT", "host": d, "value": "v=spf1 mx a -all",
                 "note": "SPF：仅允许本服务器发信"},
                {"type": "TXT", "host": f"{sel}._domainkey.{d}",
                 "value": f"v=DKIM1; k=rsa; p={pub}",
                 "note": "DKIM 公钥（超过 255 字符时需分段加引号）"},
                {"type": "TXT", "host": f"_dmarc.{d}",
                 "value": f"v=DMARC1; p=quarantine; rua=mailto:postmaster@{d}",
                 "note": "DMARC 策略"}]})

        if path == "/api/admin/dns-check" and method == "GET":
            from . import dnsres
            d = cfg.domain
            return self._json({
                "mx": dnsres.resolve_mx(d), "spf": dnsres.find_txt_record(d, "v=spf1"),
                "dkim": dnsres.find_txt_record(f"{cfg.dkim_selector}._domainkey.{d}", "v=DKIM1"),
                "dmarc": dnsres.find_txt_record(f"_dmarc.{d}", "v=DMARC1"),
                "hostname_a": dnsres.resolve(cfg.hostname, "A")})

        if path == "/api/admin/dkim" and method == "POST":
            from .dkim import generate_keypair
            priv, pub = generate_keypair()
            cfg.dkim_private_path().write_bytes(priv)
            (cfg.dkim_dir / f"{cfg.dkim_selector}.public.txt").write_text(pub)
            app.reload_dkim()
            store.log("info", "admin_dkim_regen", cfg.dkim_selector, ip)
            return self._json({"ok": True, "public": pub})

        if path == "/api/admin/cert" and method == "POST":
            from .tlsutil import generate_self_signed
            generate_self_signed(Path(cfg.cert_path), Path(cfg.key_path),
                                 cfg.hostname, cfg.domain)
            store.log("info", "admin_cert_regen", cfg.hostname, ip)
            return self._json({"ok": True, "note": "已重新生成自签名证书，重启服务后生效"})

        return self._err(404, "unknown admin api")


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def serve(app):
    """启动 HTTPS 服务（守护线程）。"""
    from .tlsutil import ensure_cert, make_context
    cert, key = ensure_cert(app.cfg)
    app.tls_ctx = make_context(cert, key)

    handler = type("BoundHandler", (WebHandler,), {"app": app, "sessions": Sessions()})
    httpd = ThreadingHTTPServer(("0.0.0.0", int(app.cfg.web["port"])), handler)
    httpd.daemon_threads = True
    httpd.socket = app.tls_ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
    return httpd
