"""SelfMail 端到端冒烟测试：
在临时目录启动完整服务器（非特权端口），用标准库客户端验证
SMTP(25/465/587) / IMAP(993) / POP3(995) / Web API / DKIM / 配额 / 别名 / 队列。

运行： .venv/bin/python tests/smoke_test.py
"""
from __future__ import annotations

import email
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import imaplib
import json
import os
import poplib
import socket
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DIR = ROOT / "tmp" / "smoke"
CFG_PATH = TEST_DIR / "config.json"
PORTS = dict(smtp_in=11025, smtp_tls=11465, smtp_submit=11587,
             imap=11143, imap_tls=11993, pop3=11110, pop3_tls=11995, web=18443)
DOMAIN = "smoke.local"
HOSTNAME = "mail.smoke.local"
ADMIN_PW = "Admin-Pass-9"
TOM_PW = "Tom-Pass-9"

results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def make_multipart(fields: dict, files: list) -> bytes:
    b = "----SelfMailSmoke" + str(time.time_ns())
    out = b""
    for k, v in fields.items():
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for name, data in files:
        out += (f"--{b}\r\nContent-Disposition: form-data; name=\"attachments\"; "
                f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


class WebClient:
    def __init__(self, base, user, pw):
        self.base = base
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.ctx = ctx
        self.cookie = None
        self._cookie = None
        self.csrf = ""
        d = self.req("/api/login", json={"user": user, "pass": pw})
        self.user, self.admin, self.csrf = d["user"], d.get("admin"), d["csrf"]

    def req(self, path, json_body=None, form=None, method=None, raw=False, json=None):
        if json is not None and json_body is None:
            json_body = json
        if json_body is not None:
            import json as _json
            data = _json.dumps(json_body).encode()
            headers = {"Content-Type": "application/json", "X-CSRF": self.csrf}
            method = method or "POST"
        elif form is not None:
            data, ctype = make_multipart(form[0], form[1])
            headers = {"Content-Type": ctype, "X-CSRF": self.csrf}
            method = method or "POST"
        else:
            data, headers, method = None, {"X-CSRF": self.csrf}, method or "GET"
        if self.cookie:
            headers["Cookie"] = self.cookie
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        resp = urllib.request.urlopen(r, context=self.ctx, timeout=30)
        body = resp.read()
        sc = resp.headers.get("Set-Cookie")
        if sc and sc.startswith("sm_session=") and "Max-Age=0" not in sc:
            self._cookie = sc.split(";")[0]
            self.cookie = self._cookie
        return body if raw else __import__("json").loads(body)


def wait_port(port, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), 2):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _kill_stale_servers():
    """清理上次遗留的测试服务器（防止端口被旧实例占用）。"""
    import glob
    me = os.getpid()
    for stat in glob.glob("/proc/[0-9]*/cmdline"):
        pid = int(stat.split("/")[2])
        if pid == me:
            continue
        try:
            cmd = open(stat, "rb").read().decode("utf-8", "replace").replace("\x00", " ")
        except OSError:
            continue
        if "selfmail" in cmd and " serve" in cmd and "smoke_test" not in cmd:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def main() -> int:
    import shutil
    _kill_stale_servers()
    time.sleep(0.3)
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True)

    cfg = {
        "domain": DOMAIN, "hostname": HOSTNAME,
        "data_dir": str(TEST_DIR / "data"),
        "smtp": {"port_in": PORTS["smtp_in"], "port_tls": PORTS["smtp_tls"],
                 "port_submit": PORTS["smtp_submit"], "max_size_mb": 25},
        "imap": {"port": PORTS["imap"], "port_tls": PORTS["imap_tls"]},
        "pop3": {"port": PORTS["pop3"], "port_tls": PORTS["pop3_tls"]},
        "web": {"port": PORTS["web"]},
        "tls": {"cert": str(TEST_DIR / "data" / "certs" / "cert.pem"),
                "key": str(TEST_DIR / "data" / "certs" / "key.pem")},
        "outbound": {"mode": "disabled"},
    }
    CFG_PATH.write_text(json.dumps(cfg, indent=2))

    py = sys.executable
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    print("== 启动服务器 ==")
    proc = subprocess.Popen([py, "-m", "selfmail", "--config", str(CFG_PATH), "serve"],
                            cwd=str(ROOT), env=env,
                            stdout=open("tmp/smoke/server_stdout.log","w"), stderr=subprocess.STDOUT, text=True)
    try:
        ok = wait_port(PORTS["web"], 40)
        check("服务器启动并监听 Web 端口", ok)
        if not ok:
            print(proc.stdout.read() if proc.stdout else "")
            return 1

        # CLI 建账号
        r = subprocess.run([py, "-m", "selfmail", "--config", str(CFG_PATH),
                            "adduser", "admin", ADMIN_PW, "--admin"],
                           cwd=str(ROOT), env=env, capture_output=True, text=True)
        check("CLI 创建管理员", r.returncode == 0, r.stderr[-200:])
        r = subprocess.run([py, "-m", "selfmail", "--config", str(CFG_PATH), "adduser", "tom", TOM_PW],
                           cwd=str(ROOT), env=env, capture_output=True, text=True)
        check("CLI 创建普通账号", r.returncode == 0, r.stderr[-200:])
        r = subprocess.run([py, "-m", "selfmail", "--config", str(CFG_PATH),
                            "alias", "add", "sales", "tom"],
                           cwd=str(ROOT), env=env, capture_output=True, text=True)
        check("CLI 创建别名 sales -> tom", r.returncode == 0, r.stderr[-200:])

        base = f"https://127.0.0.1:{PORTS['web']}"

        print("== SMTP ==")
        # 587：无 TLS 不允许 AUTH
        s = smtplib.SMTP("127.0.0.1", PORTS["smtp_submit"], timeout=15)
        code, _ = s.docmd("AUTH", "PLAIN " + __import__("base64").b64encode(
            b"\0tom\0wrongpw").decode())
        check("587 明文 AUTH 被拒绝(530)", code in (530, 538), f"got {code}")
        s.close()

        s = smtplib.SMTP("127.0.0.1", PORTS["smtp_submit"], timeout=15)
        s.starttls(context=ssl._create_unverified_context())
        try:
            s.login("tom", "badpw")
            check("错误密码被拒", False)
        except smtplib.SMTPAuthenticationError:
            check("错误密码被拒", True)
        try:
            s.login("tom", TOM_PW)
            check("587 STARTTLS + 登录成功", True)
        except Exception as e:  # noqa: BLE001
            check("587 STARTTLS + 登录成功", False, str(e))
        # 本域投递（别名收件人）
        m1 = MIMEMultipart()
        m1["From"] = "tom@%s" % DOMAIN
        m1["To"] = "sales@%s" % DOMAIN
        m1["Subject"] = "alias-test-你好"
        m1.attach(MIMEText("正文：经由别名投递测试", "plain", "utf-8"))
        att = MIMEApplication(b"PDFDATA-1234567890", _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename="report.pdf")
        m1.attach(att)
        s.sendmail("tom@" + DOMAIN, ["sales@" + DOMAIN], m1.as_string())
        check("经别名向本域投递成功（含附件）", True)
        # 开放中继被拒
        try:
            s.sendmail("tom@" + DOMAIN, ["stranger@evil.example"], m1.as_string())
            # 注意：587 已认证允许发外域 —— 属预期；真正禁止的是 25 端口未认证
            check("已认证用户可发外域（进入队列）", True)
        except Exception as e:  # noqa: BLE001
            check("已认证用户可发外域（进入队列）", False, str(e))
        s.quit()

        # 465 隐式 TLS
        s465 = smtplib.SMTP_SSL("127.0.0.1", PORTS["smtp_tls"], timeout=15,
                                context=ssl._create_unverified_context())
        s465.login("tom", TOM_PW)
        m2 = MIMEText("465 端口直发测试", "plain", "utf-8")
        m2["From"] = "tom@%s" % DOMAIN
        m2["To"] = "admin@%s" % DOMAIN
        m2["Subject"] = "port465-test"
        s465.sendmail("tom@" + DOMAIN, ["admin@" + DOMAIN], m2.as_string())
        s465.quit()
        check("465 隐式 TLS 发信成功", True)

        # 25 未认证：本域可收，外域中继拒绝
        s25 = smtplib.SMTP("127.0.0.1", PORTS["smtp_in"], timeout=15)
        m3 = MIMEText("外部世界来信", "plain", "utf-8")
        m3["From"] = "friend@remote.example"
        m3["To"] = "tom@%s" % DOMAIN
        m3["Subject"] = "mx-inbound-test"
        s25.sendmail("friend@remote.example", ["tom@" + DOMAIN], m3.as_string())
        check("25 端口外域 -> 本域投递成功", True)
        try:
            s25.sendmail("friend@remote.example", ["victim@evil.example"], m3.as_string())
            check("25 端口未认证外域中继被拒(550)", False, "relay was allowed!")
        except smtplib.SMTPRecipientsRefused:
            check("25 端口未认证外域中继被拒(550)", True)
        s25.quit()

        print("== IMAP ==")
        I = imaplib.IMAP4_SSL("127.0.0.1", PORTS["imap_tls"],
                              ssl_context=ssl._create_unverified_context())
        typ, _ = I.login("tom", TOM_PW)
        check("IMAP 993 登录", typ == "OK")
        typ, data = I.select("INBOX")
        check("SELECT INBOX", typ == "OK" and int(data[0]) >= 2, f"exists={data}")
        typ, data = I.search(None, "ALL")
        ids = data[0].split()
        check("SEARCH ALL", typ == "OK" and len(ids) >= 2)
        latest = ids[-1]
        typ, data = I.fetch(latest, "(RFC822.SIZE ENVELOPE BODYSTRUCTURE FLAGS)")
        print("   F1:", typ, repr(data)[:300])
        check("FETCH ENVELOPE/BODYSTRUCTURE",
              typ == "OK" and data and data[0] and b"ENVELOPE" in data[0])
        typ, data = I.fetch(latest, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
        print("   F2:", typ, repr(data)[:250])
        check("FETCH HEADER.FIELDS", typ == "OK" and data and data[0] and data[0][1]
              and b"Subject" in data[0][1])
        typ, data = I.fetch(ids[0], "(BODY.PEEK[])")  # 第1封=带附件的别名投递
        raw = data[0][1]
        check("FETCH BODY[] 拿到完整原始邮件", typ == "OK" and data[0][1]
              and b"report.pdf" in data[0][1])
        # 中文文件夹（UTF-7）
        cn = "&XfJT0ZAB-"  # 测试
        typ, _ = I.create(cn)
        check("CREATE 中文文件夹(UTF-7)", typ == "OK")
        typ, data = I.list()
        check("LIST 列出中文文件夹", typ == "OK" and any(b"&XfJT0ZAB-" in x for x in data))
        typ, _ = I.copy(latest, cn)
        check("COPY 到中文文件夹", typ == "OK")
        typ, data = I.select(cn)
        check("新文件夹有 1 封", typ == "OK" and int(data[0]) == 1, str(data))
        typ, data = I.uid("SEARCH", None, "ALL")
        typ, _ = I.uid("STORE", data[0].split()[0], "+FLAGS", "(\\Flagged)")
        check("UID STORE 标旗", typ == "OK")
        typ, data = I.search(None, "FLAGGED")
        check("SEARCH FLAGGED", typ == "OK" and len(data[0].split()) == 1)
        # APPEND
        draft = ("From: tom@%s\r\nTo: x@y.example\r\nSubject: appended\r\n\r\nbody-here\r\n" % DOMAIN).encode()
        typ, _ = I.append("Drafts", "(\\Draft)", None, draft)
        check("APPEND 草稿", typ == "OK")
        typ, data = I.select("Drafts")
        check("Drafts 有 1 封", typ == "OK" and int(data[0]) == 1)
        # IDLE 基本握手
        I.sock.sendall(b"a1 IDLE\r\n")
        resp = I.sock.recv(64)
        I.sock.sendall(b"DONE\r\n")
        time.sleep(0.3)
        I.sock.recv(4096)
        check("IDLE 进入/退出握手", resp.startswith(b"+"))
        typ, _ = I.logout()
        check("IMAP LOGOUT", typ in ("OK", "BYE"))  # imaplib 对快速关闭返回 BYE

        print("== POP3 ==")
        P = poplib.POP3_SSL("127.0.0.1", PORTS["pop3_tls"],
                            context=ssl._create_unverified_context())
        P.user("tom")
        P.pass_(TOM_PW)
        n, size = P.stat()
        check("POP3 STAT", n >= 2, f"n={n}")
        uidl = P.uidl()[1]
        check("POP3 UIDL", len(uidl) == n)
        retr = P.retr(n)
        check("POP3 RETR", b"mx-inbound-test" in b"\r\n".join(retr[1]))
        top = P.top(n, 3)
        check("POP3 TOP", len(top[1]) >= 3)
        P.dele(n)
        P.rset()
        P.quit()
        check("POP3 DELE/RSET/QUIT", True)

        print("== Web API ==")
        admin = WebClient(base, "admin", ADMIN_PW)
        check("Web 登录(管理员)", admin.user == "admin" and admin.admin)
        me = admin.req("/api/me")
        check("/api/me 返回配额信息", me["quota_mb"] > 0 and "csrf" in me)

        # Webmail 发信（本地 + 出站队列）
        r = admin.req("/api/send", form=({
            "to": f"tom@{DOMAIN}, outsider@remote.example",
            "subject": "web-send-test", "body": "来自 Webmail 的邮件"}, [
            ("hello.txt", b"FILE-CONTENT-XYZ")]))
        check("Webmail 发信（本地投递 + 出站排队）", r["ok"] and r["local"] == 1 and r["queued"] == 1, str(r))

        tom = WebClient(base, "tom", TOM_PW)
        msgs = tom.req("/api/messages?folder=INBOX")
        check("tom 收件箱列表", len(msgs) >= 3)
        target = next(m for m in msgs if m["subject"] == "web-send-test")
        detail = tom.req(f"/api/message/{target['id']}")
        check("读信（正文/附件列表/已读标记）",
              "来自 Webmail" in detail["text"] and detail["attachments"]
              and "\\Seen" in detail["flags"])
        atts = detail["attachments"]
        att_body = tom.req(f"/api/attachment/{target['id']}/{atts[0]['part']}", raw=True)
        check("下载附件内容一致", att_body == b"FILE-CONTENT-XYZ")

        # 别名：给 sales 发信应进 tom 的 INBOX
        got = tom.req("/api/messages?folder=INBOX&q=alias-test")
        check("别名收信出现在 tom 收件箱", len(got) >= 1)
        det = tom.req(f"/api/message/{got[0]['id']}")
        check("正文含中文且完整", "经由别名投递测试" in det["text"])

        tom.req("/api/flags", method="POST", json={"id": target["id"], "flag": "\\Flagged", "on": True})
        msgs2 = tom.req("/api/messages?folder=INBOX")
        check("标旗生效", next(m for m in msgs2 if m["id"] == target["id"])["flags"].count("\\Flagged") == 1)
        tom.req("/api/move", method="POST", json={"id": target["id"], "to": "Archive"})
        arch = tom.req("/api/messages?folder=Archive")
        check("移动到 Archive", any(m["subject"] == "web-send-test" for m in arch))
        tom.req("/api/folders", method="POST", json={"name": "工作"})
        fl = tom.req("/api/folders")
        check("Web 创建中文文件夹", any(f["name"] == "工作" for f in fl))

        # 管理端
        users = admin.req("/api/admin/users")
        check("管理端用户列表", {u["username"] for u in users} >= {"admin", "tom"})
        status = admin.req("/api/admin/status")
        check("状态面板", status["domain"] == DOMAIN and status["queue"] >= 1)
        dns = admin.req("/api/admin/dns")
        check("DNS 向导生成 5 条记录", len(dns["records"]) == 5 and
              any(r["type"] == "MX" for r in dns["records"]))
        check("DKIM 公钥已生成", any("v=DKIM1" in r["value"] and len(r["value"]) > 300
                                     for r in dns["records"]))
        q = admin.req("/api/admin/queue")
        check("出站队列有积压(outbound=disabled)", any(j["rcpt"] == "outsider@remote.example" for j in q))
        logs = admin.req("/api/admin/logs?n=100")
        check("日志可查询", len(logs) >= 5)
        # 权限
        try:
            tom.req("/api/admin/users")
            check("普通用户禁入管理端", False)
        except Exception:  # noqa: BLE001
            check("普通用户禁入管理端", True)
        # CSRF
        r = urllib.request.Request(base + "/api/flags", data=b"{}",
                                   headers={"Content-Type": "application/json",
                                            "Cookie": tom.cookie}, method="POST")
        try:
            urllib.request.urlopen(r, context=admin.ctx, timeout=10)
            check("无 CSRF 头被拒(403)", False)
        except urllib.error.HTTPError as e:
            check("无 CSRF 头被拒(403)", e.code == 403, str(e.code))
        # 改密
        tom.req("/api/password", method="POST", json={"old": TOM_PW, "new": "New-Tom-Pass-7"})
        try:
            WebClient(base, "tom", TOM_PW)
            check("旧密码失效", False)
        except Exception:  # noqa: BLE001
            check("旧密码失效", True)
        WebClient(base, "tom", "New-Tom-Pass-7")
        check("新密码可登录", True)

        print("== DKIM ==")
        from selfmail import dkim as _dkim
        from selfmail import config as _config
        cfg_obj = _config.load(CFG_PATH)
        pub = _dkim.ensure_keys(cfg_obj)
        check("DKIM 密钥生成", len(pub) > 200)
        test_raw = (f"From: a@{DOMAIN}\r\nTo: b@{DOMAIN}\r\nSubject: d=1\r\n\r\nhello dkim\r\n").encode()
        signed = _dkim.sign(test_raw, DOMAIN, cfg_obj.dkim_selector,
                            cfg_obj.dkim_private_path().read_bytes())
        check("DKIM-Signature 头已插入", signed.startswith(b"DKIM-Signature:"))
        try:
            import dkim as dkimpy
            pub = cfg_obj.dkim_private_path().with_name(
                cfg_obj.dkim_selector + ".public.txt").read_text().strip()
            dom, sel = cfg_obj.domain, cfg_obj.dkim_selector

            def fake_txt(name, timeout=5):
                n = name.decode() if isinstance(name, bytes) else str(name)
                if n.startswith(f"{sel}._domainkey.{dom}"):
                    return f"v=DKIM1; k=rsa; p={pub}".encode()
                return b""

            logging.basicConfig(level=logging.DEBUG)
            ok = dkimpy.verify(signed, dnsfunc=fake_txt)
            check("dkimpy 独立验签通过", ok is True)
        except ImportError:
            print("  - (未安装 dkimpy，跳过独立验签)")

        print("== 磁盘存储 ==")
        mail_root = TEST_DIR / "data" / "mail"
        check("邮件以 .eml 文件落盘", any(mail_root.rglob("*.eml")))
        db = TEST_DIR / "data" / "selfmail.db"
        db_size = sum(f.stat().st_size for f in
                      [db, db.with_suffix(".db-wal"), db.with_suffix(".db-shm")]
                      if f.exists())
        check("SQLite 元数据库存在", db.exists() and db_size > 10000, f"size={db_size}")

    finally:
        proc.terminate()
        try:
            out = proc.stdout.read() if proc.stdout else ""
            if "--VERBOSE-LOG--" in os.environ:
                print(out[-3000:])
        except Exception:  # noqa: BLE001
            pass

    fails = [r for r in results if not r[1]]
    print(f"\n======== 结果：{len(results) - len(fails)}/{len(results)} 通过 ========")
    for name, ok, detail in fails:
        print(f"  FAIL: {name}  {detail}")
    return 0 if not fails else 1


if __name__ == "__main__":
    import urllib.error  # noqa: F401
    import logging
    sys.exit(main())
