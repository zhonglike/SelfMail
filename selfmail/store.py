"""存储层：SQLite 元数据 + 磁盘原始邮件文件 + 出站队列 + 日志。
所有线程/协程共用一个带锁连接，规模（10-50 账号）下足够。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DEFAULT_FOLDERS = ["INBOX", "Sent", "Drafts", "Trash", "Archive"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  pw TEXT NOT NULL,
  display_name TEXT DEFAULT '',
  is_admin INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  quota_mb INTEGER DEFAULT 2048,
  used INTEGER DEFAULT 0,
  created TEXT
);
CREATE TABLE IF NOT EXISTS aliases(
  alias TEXT PRIMARY KEY,
  target TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user TEXT NOT NULL,
  name TEXT NOT NULL,
  uidvalidity INTEGER NOT NULL,
  uidnext INTEGER DEFAULT 1,
  UNIQUE(user, name)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user TEXT NOT NULL,
  folder TEXT NOT NULL,
  uid INTEGER NOT NULL,
  flags TEXT DEFAULT '',
  size INTEGER NOT NULL,
  msg_from TEXT DEFAULT '',
  msg_to TEXT DEFAULT '',
  subject TEXT DEFAULT '',
  date TEXT DEFAULT '',
  internaldate TEXT DEFAULT '',
  has_attach INTEGER DEFAULT 0,
  text TEXT DEFAULT '',
  file TEXT NOT NULL,
  UNIQUE(user, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_msg_folder ON messages(user, folder);
CREATE TABLE IF NOT EXISTS queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT NOT NULL,
  rcpt TEXT NOT NULL,
  rawfile TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  next_try REAL DEFAULT 0,
  last_error TEXT DEFAULT '',
  status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  event TEXT NOT NULL,
  detail TEXT DEFAULT '',
  ip TEXT DEFAULT ''
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "Z"


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.mail_dir = self.data_dir / "mail"
        self.queue_dir = self.data_dir / "queue"
        for d in (self.data_dir, self.mail_dir, self.queue_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "selfmail.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self.lock = threading.RLock()
        # 事件广播：IDLE 推送用  (user, folder) -> set(threading.Event)
        self._waiters: dict[tuple[str, str], set] = {}
        self._wlock = threading.Lock()

    # ---------- 基础工具 ----------
    def _q(self, sql, args=(), commit=False):
        with self.lock:
            cur = self.db.execute(sql, args)
            if commit:
                self.db.commit()
            return cur

    def notify(self, user: str, folder: str):
        """广播某文件夹有新变化（SMTP 收信 / web 改动 → IMAP IDLE）。"""
        with self._wlock:
            evs = list(self._waiters.get((user.lower(), folder.lower()), set()))
        for ev in evs:
            ev.set()

    def wait_change(self, user: str, folder: str, timeout: float):
        ev = threading.Event()
        key = (user.lower(), folder.lower())
        with self._wlock:
            self._waiters.setdefault(key, set()).add(ev)
        try:
            return ev.wait(timeout)
        finally:
            with self._wlock:
                self._waiters.get(key, set()).discard(ev)

    # ---------- 账号 ----------
    def create_user(self, username: str, password: str, display_name: str = "",
                    is_admin: bool = False, quota_mb: int = 2048) -> bool:
        from .auth import hash_password
        username = username.lower().strip()
        with self.lock:
            if self.db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                return False
            self.db.execute(
                "INSERT INTO users(username,pw,display_name,is_admin,active,quota_mb,used,created)"
                " VALUES(?,?,?,?,1,?,0,?)",
                (username, hash_password(password), display_name, int(is_admin), quota_mb, _now()))
            for f in DEFAULT_FOLDERS:
                self.db.execute(
                    "INSERT OR IGNORE INTO folders(user,name,uidvalidity,uidnext) VALUES(?,?,?,1)",
                    (username, f, int(time.time())))
            self.db.commit()
        self.log("info", "user_created", username)
        return True

    def delete_user(self, username: str):
        username = username.lower().strip()
        with self.lock:
            self.db.execute("DELETE FROM users WHERE username=?", (username,))
            self.db.execute("DELETE FROM folders WHERE user=?", (username,))
            for row in self.db.execute("SELECT file FROM messages WHERE user=?", (username,)).fetchall():
                try:
                    Path(row["file"]).unlink(missing_ok=True)
                except OSError:
                    pass
            self.db.execute("DELETE FROM messages WHERE user=?", (username,))
            self.db.commit()
        import shutil
        shutil.rmtree(self.mail_dir / username, ignore_errors=True)
        self.log("info", "user_deleted", username)

    def get_user(self, username: str):
        return self._q("SELECT * FROM users WHERE username=?", (username.lower().strip(),)).fetchone()

    def list_users(self):
        return self._q("SELECT username,display_name,is_admin,active,quota_mb,used,created FROM users ORDER BY username").fetchall()

    def verify_login(self, username: str, password: str) -> bool:
        row = self.get_user(username)
        if not row or not row["active"]:
            return False
        from .auth import verify_password
        return verify_password(password, row["pw"])

    def set_password(self, username: str, password: str):
        from .auth import hash_password
        self._q("UPDATE users SET pw=? WHERE username=?", (hash_password(password), username.lower()), commit=True)

    def update_user(self, username: str, **fields):
        allowed = {"display_name", "is_admin", "active", "quota_mb"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                args.append(v)
        if sets:
            args.append(username.lower())
            self._q(f"UPDATE users SET {','.join(sets)} WHERE username=?", args, commit=True)

    def is_admin(self, username: str) -> bool:
        row = self.get_user(username)
        return bool(row and row["is_admin"])

    def quota_of(self, username: str):
        row = self.get_user(username)
        return (row["quota_mb"], row["used"]) if row else (0, 0)

    def quota_ok(self, username: str, add_bytes: int) -> bool:
        row = self.get_user(username)
        if not row:
            return False
        if row["quota_mb"] <= 0:
            return True
        return row["used"] + add_bytes <= row["quota_mb"] * 1024 * 1024

    def add_usage(self, username: str, delta: int):
        self._q("UPDATE users SET used=MAX(0, used+?) WHERE username=?", (delta, username.lower()), commit=True)

    # ---------- 别名 ----------
    def add_alias(self, alias: str, target: str) -> bool:
        alias = alias.lower().strip()
        with self.lock:
            if self.db.execute("SELECT 1 FROM aliases WHERE alias=?", (alias,)).fetchone():
                return False
            self.db.execute("INSERT INTO aliases VALUES(?,?)", (alias, target.lower().strip()))
            self.db.commit()
        return True

    def del_alias(self, alias: str):
        self._q("DELETE FROM aliases WHERE alias=?", (alias.lower().strip(),), commit=True)

    def list_aliases(self):
        return self._q("SELECT alias,target FROM aliases ORDER BY alias").fetchall()

    def resolve_rcpt(self, rcpt: str) -> str | None:
        """收件人 → 本地账号名。支持别名；非本域返回 None。"""
        rcpt = rcpt.lower().strip().strip("<>")
        if "@" not in rcpt:
            return None
        local, domain = rcpt.rsplit("@", 1)
        if self.get_user(local):  # 本域账号形式 user@domain 或直接 user
            return local
        row = self._q("SELECT target FROM aliases WHERE alias=?", (rcpt,)).fetchone()
        if row:
            t = row["target"]
            return t.rsplit("@", 1)[0] if "@" in t else t
        row = self._q("SELECT target FROM aliases WHERE alias=?", (local,)).fetchone()
        if row:
            t = row["target"]
            return t.rsplit("@", 1)[0] if "@" in t else t
        return None

    def is_local_domain(self, domain: str) -> bool:
        from .config import Config  # 由 serve() 注入 self.domains
        return domain.lower() in getattr(self, "domains", set())

    # ---------- 文件夹 ----------
    def list_folders(self, user: str):
        return self._q("SELECT name,uidvalidity,uidnext FROM folders WHERE user=? ORDER BY rowid", (user.lower(),)).fetchall()

    def ensure_folder(self, user: str, name: str) -> bool:
        with self.lock:
            if self.db.execute("SELECT 1 FROM folders WHERE user=? AND name=?", (user.lower(), name)).fetchone():
                return False
            self.db.execute("INSERT INTO folders(user,name,uidvalidity) VALUES(?,?,?)",
                            (user.lower(), name, int(time.time())))
            self.db.commit()
        return True

    def delete_folder(self, user: str, name: str) -> bool:
        if name in DEFAULT_FOLDERS:
            return False
        with self.lock:
            rows = self.db.execute("SELECT file FROM messages WHERE user=? AND folder=?", (user.lower(), name)).fetchall()
            self.db.execute("DELETE FROM messages WHERE user=? AND folder=?", (user.lower(), name))
            self.db.execute("DELETE FROM folders WHERE user=? AND name=?", (user.lower(), name))
            self.db.commit()
        for r in rows:
            Path(r["file"]).unlink(missing_ok=True)
        self.notify(user, name)
        return True

    def folder_status(self, user: str, name: str):
        with self.lock:
            row = self._q(
                "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes, SUM(flags NOT LIKE '%\\\\Seen%') unseen,"
                " SUM(flags LIKE '%\\\\Deleted%') del FROM messages WHERE user=? AND folder=?",
                (user.lower(), name)).fetchone()
        return {"exists": True, "messages": row["n"], "size": row["bytes"],
                "unseen": row["unseen"] or 0, "deleted": row["del"] or 0}

    def folder_ids(self, user: str, name: str):
        """按 uid 升序返回本文件夹全部消息行。"""
        return self._q("SELECT * FROM messages WHERE user=? AND folder=? AND flags NOT LIKE '%\\\\Deleted%' ORDER BY uid",
                       (user.lower(), name)).fetchall()

    # ---------- 邮件投递 / 读写 ----------
    def deliver(self, user: str, folder: str, raw: bytes, flags: str = "",
                msg_from: str = "", msg_to: str = "", subject: str = "",
                date: str = "", text: str = "", has_attach: bool = False) -> int:
        """写入一封邮件，返回消息 id。"""
        user = user.lower()
        with self.lock:
            f = self._q("SELECT id,uidnext FROM folders WHERE user=? AND name=?", (user, folder)).fetchone()
            if not f:
                self.ensure_folder(user, folder)
                f = self._q("SELECT id,uidnext FROM folders WHERE user=? AND name=?", (user, folder)).fetchone()
            uid = f["uidnext"]
            self.db.execute("UPDATE folders SET uidnext=uidnext+1 WHERE id=?", (f["id"],))
            fdir = self.mail_dir / user / f"f{f['id']}"
            fdir.mkdir(parents=True, exist_ok=True)
            fpath = fdir / f"{uid:06d}.eml"
            fpath.write_bytes(raw)
            cur = self.db.execute(
                "INSERT INTO messages(user,folder,uid,flags,size,msg_from,msg_to,subject,date,internaldate,has_attach,text,file)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user, folder, uid, flags, len(raw), msg_from or "", msg_to or "",
                 subject or "", date or "", _now(), int(has_attach), (text or "")[:20000], str(fpath)))
            self.db.execute("UPDATE users SET used=used+? WHERE username=?", (len(raw), user))
            mid = cur.lastrowid
            self.db.commit()
        self.notify(user, folder)
        return mid

    def get_message(self, user: str, mid: int):
        return self._q("SELECT * FROM messages WHERE user=? AND id=?", (user.lower(), mid)).fetchone()

    def get_message_by_uid(self, user: str, folder: str, uid: int):
        return self._q("SELECT * FROM messages WHERE user=? AND folder=? AND uid=?",
                       (user.lower(), folder, uid)).fetchone()

    def read_raw(self, row) -> bytes:
        return Path(row["file"]).read_bytes()

    def set_flags(self, user: str, mid: int, flags: str):
        self._q("UPDATE messages SET flags=? WHERE user=? AND id=?", (flags, user.lower(), mid), commit=True)
        row = self.get_message(user, mid)
        if row:
            self.notify(user, row["folder"])

    def copy_message(self, user: str, mid: int, dest_folder: str) -> int | None:
        row = self.get_message(user, mid)
        if not row:
            return None
        return self.deliver(user, dest_folder, self.read_raw(row), row["flags"],
                            row["msg_from"], row["msg_to"], row["subject"],
                            row["date"], row["text"], bool(row["has_attach"]))

    def delete_message(self, user: str, mid: int) -> bool:
        """进入 Trash；若已在 Trash 则物理删除。"""
        row = self.get_message(user, mid)
        if not row:
            return False
        if row["folder"] == "Trash":
            return self.expunge_id(user, mid)
        self.copy_message(user, mid, "Trash")
        return self.expunge_id(user, mid)

    def expunge_id(self, user: str, mid: int) -> bool:
        row = self.get_message(user, mid)
        if not row:
            return False
        with self.lock:
            self.db.execute("DELETE FROM messages WHERE id=?", (mid,))
            self.db.execute("UPDATE users SET used=MAX(0,used-?) WHERE username=?", (row["size"], user.lower()))
            self.db.commit()
        Path(row["file"]).unlink(missing_ok=True)
        self.notify(user, row["folder"])
        return True

    def expunge_deleted(self, user: str, folder: str) -> int:
        rows = self._q("SELECT id,size,file FROM messages WHERE user=? AND folder=? AND flags LIKE '%\\\\Deleted%'",
                       (user.lower(), folder)).fetchall()
        with self.lock:
            self.db.execute("DELETE FROM messages WHERE user=? AND folder=? AND flags LIKE '%\\\\Deleted%'",
                            (user.lower(), folder))
            self.db.commit()
        total = 0
        for r in rows:
            total += r["size"]
            Path(r["file"]).unlink(missing_ok=True)
            self._q("UPDATE users SET used=MAX(0,used-?) WHERE username=?", (r["size"], user.lower()), commit=True)
        self.notify(user, folder)
        return len(rows)

    def search_messages(self, user: str, folder: str, q: str, unread_only: bool = False):
        like = f"%{q}%"
        sql = ("SELECT * FROM messages WHERE user=? AND folder=? AND flags NOT LIKE '%\\\\Deleted%'")
        args: list = [user.lower(), folder]
        if unread_only:
            sql += " AND flags NOT LIKE '%\\\\Seen%'"
        if q:
            sql += " AND (subject LIKE ? OR msg_from LIKE ? OR msg_to LIKE ? OR text LIKE ?)"
            args += [like, like, like, like]
        sql += " ORDER BY uid DESC"
        return self._q(sql, args).fetchall()

    # ---------- 出站队列 ----------
    def enqueue(self, sender: str, rcpt: str, raw: bytes) -> int:
        fname = f"{uuid.uuid4().hex}.eml"
        (self.queue_dir / fname).write_bytes(raw)
        cur = self._q("INSERT INTO queue(sender,rcpt,rawfile,next_try,status) VALUES(?,?,?,?, 'pending')",
                      (sender, rcpt, fname, time.time()), commit=True)
        self.log("info", "queued", f"{sender} -> {rcpt}")
        return cur.lastrowid

    def due_queue(self, limit: int = 50):
        return self._q("SELECT * FROM queue WHERE status='pending' AND next_try<=? ORDER BY id LIMIT ?",
                       (time.time(), limit)).fetchall()

    def queue_done(self, qid: int):
        row = self._q("SELECT rawfile FROM queue WHERE id=?", (qid,)).fetchone()
        if row:
            (self.queue_dir / row["rawfile"]).unlink(missing_ok=True)
        self._q("DELETE FROM queue WHERE id=?", (qid,), commit=True)

    def queue_fail(self, qid: int, err: str, max_attempts: int) -> str:
        """记录失败并退避重试；超过次数置 bounced。返回最终状态。"""
        row = self._q("SELECT attempts FROM queue WHERE id=?", (qid,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        if attempts >= max_attempts:
            self._q("UPDATE queue SET status='bounced', attempts=?, last_error=? WHERE id=?",
                    (attempts, err[:500], qid), commit=True)
            self.log("warn", "bounce", f"queue#{qid}: {err[:200]}")
            return "bounced"
        backoff = min(15 * (4 ** (attempts - 1)), 720) * 60  # 15m,1h,4h,16h…
        self._q("UPDATE queue SET attempts=?, last_error=?, next_try=? WHERE id=?",
                (attempts, err[:500], time.time() + backoff, qid), commit=True)
        return "pending"

    def drop_queue(self, qid: int):
        with self.lock:
            row = self._q("SELECT rawfile FROM queue WHERE id=?", (qid,)).fetchone()
            self._q("DELETE FROM queue WHERE id=?", (qid,), commit=True)
        if row:
            try:
                (self.queue_dir / row["rawfile"]).unlink(missing_ok=True)
            except OSError:
                pass

    def queue_clear(self, qid) -> int:
        """清理队列任务（qid='all' 或具体 id），返回删除数。"""
        with self.lock:
            if str(qid) == "all":
                rows = self._q("SELECT rawfile FROM queue").fetchall()
                self._q("DELETE FROM queue", commit=True)
            else:
                rows = self._q("SELECT rawfile FROM queue WHERE id=?", (int(qid),)).fetchall()
                self._q("DELETE FROM queue WHERE id=?", (int(qid),), commit=True)
        for r in rows:
            try:
                (self.queue_dir / r["rawfile"]).unlink(missing_ok=True)
            except OSError:
                pass
        return len(rows)

    def queue_retry(self, qid) -> int:
        """重置 attempts/next_try，立即重投（bounced 除外）。"""
        with self.lock:
            if str(qid) == "all":
                cur = self._q("UPDATE queue SET attempts=0, next_try=0, last_error='' "
                              "WHERE status='pending'", commit=True)
            else:
                cur = self._q("UPDATE queue SET attempts=0, next_try=0, last_error='' "
                              "WHERE id=? AND status='pending'", (int(qid),), commit=True)
            return cur.rowcount if cur else 0

    def rename_folder(self, user: str, old: str, new: str) -> bool:
        old, new = old.strip(), new.strip()
        if not new or old in DEFAULT_FOLDERS or "/" in new:
            return False
        with self.lock:
            dup = self.db.execute("SELECT 1 FROM folders WHERE user=? AND name=?",
                                  (user.lower(), new)).fetchone()
            if dup:
                return False
            self.db.execute("UPDATE folders SET name=? WHERE user=? AND name=?",
                            (new, user.lower(), old))
            self.db.execute("UPDATE messages SET folder=? WHERE user=? AND folder=?",
                            (new, user.lower(), old))
            self.db.commit()
        self.notify(user, new)
        return True

    def retry_queue(self, qid: int):
        self._q("UPDATE queue SET status='pending', attempts=0, next_try=?, last_error='' "
                "WHERE id=?", (time.time(), qid), commit=True)

    def list_queue(self):
        return self._q("SELECT * FROM queue ORDER BY id DESC LIMIT 200").fetchall()

    # ---------- 日志 ----------
    def log(self, level: str, event: str, detail: str = "", ip: str = ""):
        with self.lock:
            self.db.execute("INSERT INTO logs(ts,level,event,detail,ip) VALUES(?,?,?,?,?)",
                            (time.time(), level, event, detail[:500], ip))
            self.db.commit()
        if level in ("warn", "error"):
            print(f"[{level}] {event}: {detail[:200]}", flush=True)

    def recent_logs(self, n: int = 200, level: str = ""):
        if level:
            return self._q("SELECT * FROM logs WHERE level=? ORDER BY id DESC LIMIT ?", (level, n)).fetchall()
        return self._q("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def parse_message(raw: bytes):
    """解析原始邮件 → (headers dict, body_text, html, attachments[(part_key, filename, content_type, bytes)])"""
    import email
    from email import policy
    msg = email.message_from_bytes(raw, policy=policy.default)
    body_text, html, attachments = "", "", []
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "")
        filename = part.get_filename()
        if disp == "attachment" or (filename and disp != "inline"):
            try:
                attachments.append((str(i), filename or f"file-{i}", ctype, part.get_payload(decode=True) or b""))
            except Exception:
                pass
        elif ctype == "text/plain" and not body_text:
            try:
                body_text = part.get_content()
            except Exception:
                body_text = part.get_payload(decode=True).decode("utf-8", "replace") if part.get_payload(decode=True) else ""
        elif ctype == "text/html" and not html:
            try:
                html = part.get_content()
            except Exception:
                html = ""
    hdrs = {k: str(v) for k, v in msg.items()}
    return msg, hdrs, body_text, html, attachments
