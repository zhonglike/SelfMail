"""IMAP4rev1 服务端（993 隐式 TLS / 143 STARTTLS）。
FETCH(ENVELOPE/BODY[]/BODYSTRUCTURE)、SEARCH、STORE、COPY/MOVE、APPEND、
EXPUNGE、IDLE 实时推送；文件夹名支持中文（modified UTF-7）。
"""
from __future__ import annotations

import base64
import email
import email.header
import email.utils
import re
import time

from .netutil import LineConn, serve_forever
from .store import DEFAULT_FOLDERS

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_CAPS = "IMAP4rev1 MOVE IDLE UNSELECT UIDPLUS LITERAL+ CHILDREN"


# ---------- modified UTF-7 ----------
def utf7_decode(s: str) -> str:
    """modified UTF-7 → unicode；无 & 时原样返回。"""
    if "&" not in s:
        return s
    out, i = [], 0
    while i < len(s):
        if s[i] != "&":
            out.append(s[i]); i += 1; continue
        j = s.find("-", i)
        if j == -1:
            out.append(s[i:]); break
        b64 = s[i + 1:j].replace(",", "/")
        if not b64:  # "&-" → "&"
            out.append("&"); i = j + 1; continue
        try:
            out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4))
                       .decode("utf-16-be"))
        except Exception:
            out.append(s[i:j + 1])
        i = j + 1
    return "".join(out)


def folder_in(name: str) -> str:
    """客户端提供的文件夹名（mUTF-7）→ 存储用 unicode。"""
    return utf7_decode(name.strip('"'))

def utf7_encode(s: str) -> str:
    out, buf = [], ""
    for ch in s:
        if "\x20" <= ch <= "\x7e":
            if buf:
                b = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=")
                out.append("&" + b.replace("/", ",") + "-")
                buf = ""
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    if buf:
        b = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=")
        out.append("&" + b.replace("/", ",") + "-")
    return "".join(out)


def _decode_hdr(s: str) -> str:
    try:
        parts = email.header.decode_header(s or "")
        out = ""
        for t, enc in parts:
            out += t.decode(enc or "utf-8", "replace") if isinstance(t, bytes) else t
        return out
    except Exception:
        return s or ""


def _imap_str(s: str) -> str:
    """IMAP 字符串：可安全引用则加引号，否则用字面量；空串返回 NIL。
    非 ASCII 用 surrogateescape 表示字节，最终发送时按 utf-8/surrogateescape 还原，
    避免双重编码。"""
    if not s:
        return "NIL"
    if all(0x20 <= ord(c) <= 0x7e for c in s) and '"' not in s and "\\" not in s:
        return f'"{s}"'
    b = s.encode("utf-8", "surrogateescape")
    return "{" + str(len(b)) + "}\r\n" + b.decode("utf-8", "surrogateescape")


def _envelope(msg) -> str:
    def grp(name):
        vals = msg.get_all(name) or []
        addrs = email.utils.getaddresses([str(v) for v in vals])
        if not addrs:
            return "NIL"
        items = []
        for disp, addr in addrs:
            if not disp and not addr:
                continue
            mb, _, host = addr.rpartition("@")
            items.append(f"({_imap_str(_decode_hdr(disp))} NIL {_imap_str(mb or addr)} "
                         f"{_imap_str(host)})")
        return "(" + " ".join(items) + ")" if items else "NIL"

    def s(name):
        v = _decode_hdr(str(msg.get(name, "")))
        return _imap_str(v) if v else "NIL"

    frm = grp("From") or "NIL"
    return (f"({s('Date')} {s('Subject')} {frm} {frm} {frm} {grp('To')} "
            f"{grp('Cc')} {grp('Bcc')} {s('In-Reply-To')} {s('Message-ID')})")


def _bodystructure(msg, ext: bool = True) -> str:
    def leaf(p) -> str:
        ctype = p.get_content_type()
        main, _, sub = ctype.partition("/")
        size = 0
        lines = 0
        payload = p.get_payload(decode=True)
        if payload is not None:
            size = len(payload)
            if main == "TEXT":
                lines = payload.count(b"\n") + (1 if payload and not payload.endswith(b"\n") else 0)
        params = []
        for k, v in (p.get_params() or [])[1:]:
            params += [k.upper(), v or "NIL"]
        pstr = "(" + " ".join(_imap_str(x) for x in params) + ")" if params else "NIL"
        fid = _imap_str(str(p.get("Content-ID", ""))) if p.get("Content-ID") else "NIL"
        fdesc = _imap_str(str(p.get("Content-Description", ""))) if p.get("Content-Description") else "NIL"
        enc = (p.get("Content-Transfer-Encoding") or "7BIT").split()[0].upper()
        if main == "TEXT":
            base = f'("{main.upper()}" "{sub.upper()}" {pstr} {fid} {fdesc} "{enc}" {size} {lines})'
        else:
            base = f'("{main.upper()}" "{sub.upper()}" {pstr} {fid} {fdesc} "{enc}" {size})'
        if ext:
            dsp = p.get_content_disposition()
            dparams = []
            if dsp:
                for k, v in (p.get_params() or [])[1:]:
                    dparams += [k.upper(), v or "NIL"]
            dstr = (f'("{dsp.upper()}"' + (" (" + " ".join(_imap_str(x) for x in dparams) + "))")
                    if dparams else f'("{dsp.upper()}" NIL)') if dsp else "NIL"
            base = base[:-1] + f" {dstr} NIL NIL)" if base.endswith(")") else base
        return base

    if msg.is_multipart():
        subs = " ".join(_bodystructure(sp, ext) for sp in msg.get_payload() if hasattr(sp, "get_content_type"))
        params = []
        for k, v in (msg.get_params() or [])[1:]:
            params += [k.upper(), v or "NIL"]
        pstr = "(" + " ".join(_imap_str(x) for x in params) + ")" if params else "NIL"
        sub = msg.get_content_subtype().upper()
        if ext:
            return f"({subs} \"{sub}\" {pstr} NIL NIL NIL)"
        return f"({subs} \"{sub}\")"
    return leaf(msg)


def _part_by_path(msg, path: str):
    """按 1.2.3 路径取 MIME 子部件。"""
    if not path:
        return msg
    cur = msg
    for idx_s in path.split("."):
        try:
            idx = int(idx_s)
        except ValueError:
            return None
        if cur.is_multipart():
            subs = [p for p in cur.get_payload() if hasattr(p, "get_content_type")]
            if 1 <= idx <= len(subs):
                cur = subs[idx - 1]
                continue
            return None
        if idx != 1:
            return None
        # 单部件消息按 message/rfc822 约定可再取 1
        if cur.get_content_maintype() == "message":
            pl = cur.get_payload()
            if isinstance(pl, list) and pl and hasattr(pl[0], "get_content_type"):
                cur = pl[0]
                continue
        return cur if idx == 1 else None
    return cur


class IMAPSession:
    def __init__(self, app, conn: LineConn):
        self.app = app
        self.cfg = app.cfg
        self.store = app.store
        self.conn = conn
        self.user: str | None = None
        self.ro = False
        self.folder: str | None = None
        self.rows: list = []

    # ---------- 底层 ----------
    def send(self, line: str):
        self.conn.send(line)

    def fail(self, tag: str, text: str):
        self.send(f"{tag} BAD {text}")

    def ok(self, tag: str, text: str, code: str = ""):
        self.send(f"{tag} OK {code + ' ' if code else ''}{text}")

    def no(self, tag: str, text: str, code: str = ""):
        self.send(f"{tag} NO {code + ' ' if code else ''}{text}")

    def read_command(self) -> tuple[str, str, list] | None:
        """读取一条命令；返回 (tag, 命令, 参数token列表)。token: str 或 bytes。"""
        line = self.conn.read_line(65536)
        if line is None:
            return None
        segs: list[str | bytes] = [line]
        while True:
            m = re.search(r"\{(\d+)(\+)?\}\s*$", segs[-1] if isinstance(segs[-1], str) else "")
            if not m:
                break
            n = int(m.group(1))
            if not m.group(2):
                self.conn.send("+ Ready for literal data")
            data = self.conn.read_exact(n)
            if data is None:
                return None
            segs.append(data)
            rest = self.conn.read_line(65536)
            if rest is None:
                return None
            segs.append(rest)
        head = segs[0].split(" ", 2)
        if not head or not head[0]:
            return ("*", "", [])
        tag = head[0]
        cmd = head[1].upper() if len(head) > 1 else ""
        argstr = head[2] if len(head) > 2 else ""
        toks: list[str | bytes] = _tokenize(argstr)
        lit_i = 0
        for s in segs[1:]:
            if isinstance(s, bytes):
                toks.append(s)
        return tag, cmd, toks

    # ---------- 行列表 ----------
    def _reload(self):
        if not self.folder:
            return
        self.rows = self.store._q(
            "SELECT * FROM messages WHERE user=? AND folder=? ORDER BY uid",
            (self.user, self.folder)).fetchall()

    def _seq_uids(self, spec: str, by_uid: bool) -> list[int]:
        """解析 1:5,7:* 序列集，返回消息 id（表内主键）。"""
        self._reload()
        if not self.rows:
            return []
        out = []
        total = len(self.rows)
        for part in spec.split(","):
            if ":" in part:
                a, _, b = part.partition(":")
                lo = int(a) if a not in ("*", "") else total
                hi = int(b) if b not in ("*", "") else total
                if lo > hi:
                    lo, hi = hi, lo
                lo = max(1, lo)
            else:
                if part in ("*", ""):
                    lo = hi = total
                else:
                    try:
                        lo = hi = int(part)
                    except ValueError:
                        continue
            for i in range(lo, hi + 1):
                if 1 <= i <= total:
                    row = self.rows[i - 1]
                    out.append(row["id"] if by_uid else row["id"])
        return out

    def _seq_uid_set(self, spec: str, by_uid: bool) -> set[int]:
        """解析 1:5,7,* → 序号集合（by_uid 时为 UID 集合），供 FETCH/STORE 比对。"""
        self._reload()
        rows, out = self.rows, set()
        total = len(rows)
        if not total:
            return out
        for part in spec.split(","):
            if ":" in part:
                a, _, b = part.partition(":")
                lo = total if a in ("*", "") else int(a or 1)
                hi = total if b in ("*", "") else int(b or total)
                if lo > hi:
                    lo, hi = hi, lo
            else:
                lo = hi = total if part in ("*", "") else int(part or 0)
            for n in range(max(1, lo), min(hi, total) + 1):
                row = rows[n - 1]
                out.add(row["uid"] if by_uid else n)
        return out

    def _row_by_seq(self, seq: int):
        if 1 <= seq <= len(self.rows):
            return self.rows[seq - 1]
        return None

    def _flag_set(self, flags_s: str) -> set:
        return set(flags_s.split())

    # ---------- 主循环 ----------
    def run(self):
        caps = _CAPS + ("" if self.conn.tls else " STARTTLS")
        self.send(f"* OK [CAPABILITY {caps}] SelfMail IMAP4rev1 ready")
        while True:
            cmd = self.read_command()
            if cmd is None:
                return
            tag, name, args = cmd
            if not name:
                self.fail(tag or "*", "empty command")
                continue
            fn = getattr(self, f"cmd_{name.lower()}", None)
            try:
                if fn is None:
                    self.fail(tag, f"unknown command {name}")
                else:
                    fn(tag, args)
            except (ConnectionError, TimeoutError, OSError):
                return
            except Exception as e:  # noqa: BLE001
                self.app.store.log("error", "imap_cmd", f"{name}: {e!r}")
                self.fail(tag, "internal error")

    def _need_auth(self, tag) -> bool:
        if self.user:
            return True
        self.no(tag, "please authenticate first")
        return False

    def _need_tls_auth(self, tag) -> bool:
        if self.conn.tls or self.cfg.security["allow_plain_imap_pop"]:
            return True
        self.no(tag, "TLS required (use 993 or STARTTLS)", "[PRIVACYREQUIRED]")
        return False

    # ---------- 未认证 ----------
    def cmd_capability(self, tag, args):
        caps = _CAPS + ("" if self.conn.tls else " STARTTLS")
        self.send(f"* CAPABILITY {caps}")
        self.ok(tag, "CAPABILITY completed")

    def cmd_noop(self, tag, args):
        self.ok(tag, "NOOP completed")

    def cmd_logout(self, tag, args):
        self.send("* BYE SelfMail closing")
        self.ok(tag, "LOGOUT completed")
        self.conn.close()

    def cmd_starttls(self, tag, args):
        if self.conn.tls:
            self.no(tag, "already in TLS")
            return
        self.send(f"{tag} OK Start TLS")
        if not self.conn.starttls(self.app.tls_ctx):
            self.conn.close()

    def cmd_login(self, tag, args):
        if not self._need_tls_auth(tag):
            return
        if len(args) < 2:
            self.fail(tag, "LOGIN needs user and password")
            return
        user = args[0].decode("utf-8", "replace") if isinstance(args[0], bytes) else str(args[0])
        pwd = args[1].decode("utf-8", "replace") if isinstance(args[1], bytes) else str(args[1])
        self._auth(tag, user, pwd)

    def cmd_authenticate(self, tag, args):
        if not self._need_tls_auth(tag):
            return
        mech = str(args[0]).upper() if args else ""
        if mech != "PLAIN":
            self.no(tag, "only AUTH=PLAIN supported")
            return
        b64 = str(args[1]) if len(args) > 1 else None
        if not b64:
            self.conn.send("+ ")
            b64 = self.conn.read_line(65536)
            if b64 is None:
                return
        try:
            parts = base64.b64decode(b64.strip()).split(b"\x00")
            user = parts[1].decode("utf-8", "replace")
            pwd = parts[2].decode("utf-8", "replace")
        except Exception:
            self.fail(tag, "bad base64")
            return
        self._auth(tag, user, pwd)

    def _auth(self, tag, user, pwd):
        ip = self.conn.peer_ip()
        if self.app.limiter.is_banned(ip):
            self.no(tag, "too many failures, try later")
            return
        if self.store.verify_login(user, pwd):
            self.user = user.lower().strip()
            self.app.limiter.record_ok(ip)
            self.store.log("info", "imap_auth", self.user, ip)
            self.ok(tag, f"logged in as {self.user}")
        else:
            self.app.limiter.record_fail(ip)
            self.store.log("warn", "imap_auth_fail", user or "?", ip)
            self.no(tag, "invalid credentials")

    # ---------- 文件夹 ----------
    def _folder_names(self) -> list[str]:
        return [r["name"] for r in self.store.list_folders(self.user)]

    def cmd_select(self, tag, args, ro=False):
        if not self._need_auth(tag):
            return
        if not args:
            self.fail(tag, "folder name required")
            return
        name = args[0].decode("utf-8", "replace") if isinstance(args[0], bytes) else str(args[0])
        name = folder_in(name)
        if name.upper() == "INBOX":
            name = "INBOX"
        frow = next((r for r in self.store.list_folders(self.user) if r["name"] == name), None)
        if not frow:
            self.no(tag, "no such folder")
            return
        self.folder = name
        self.ro = ro
        self._reload()
        unseen = next((i + 1 for i, r in enumerate(self.rows)
                       if "\\Seen" not in r["flags"]), None)
        self.send(f"* {len(self.rows)} EXISTS")
        self.send("* 0 RECENT")
        self.send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self.send("* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft \\*)] flags")
        self.send(f"* OK [UIDVALIDITY {frow['uidvalidity']}] UIDs valid")
        self.send(f"* OK [UIDNEXT {frow['uidnext']}] next uid")
        if unseen:
            self.send(f"* OK [UNSEEN {unseen}] first unseen")
        self.ok(tag, f"[{'READ-ONLY' if ro else 'READ-WRITE'}] {name} selected")

    def cmd_examine(self, tag, args):
        self.cmd_select(tag, args, ro=True)

    def cmd_create(self, tag, args):
        if not self._need_auth(tag):
            return
        name = folder_in(_arg_str(args[0]))
        if not name or name.upper() == "INBOX":
            self.no(tag, "cannot create")
            return
        self.store.ensure_folder(self.user, name)
        self.ok(tag, "created")

    def cmd_delete(self, tag, args):
        if not self._need_auth(tag):
            return
        name = folder_in(_arg_str(args[0]))
        if not self.store.delete_folder(self.user, name):
            self.no(tag, "cannot delete this folder")
        else:
            self.ok(tag, "deleted")

    def cmd_rename(self, tag, args):
        self.no(tag, "RENAME not supported; create and MOVE instead")

    def cmd_subscribe(self, tag, args):
        self.ok(tag, "SUBSCRIBE completed")

    def cmd_unsubscribe(self, tag, args):
        self.ok(tag, "UNSUBSCRIBE completed")

    def _match_list(self, pattern: str, name: str) -> bool:
        if pattern.upper() == "INBOX" and name.upper() == "INBOX":
            return True
        rx = re.escape(pattern).replace(re.escape("*"), ".*").replace(re.escape("%"), "[^/]*")
        return re.fullmatch(rx, name, re.IGNORECASE) is not None

    def cmd_list(self, tag, args):
        if not self._need_auth(tag):
            return
        ref = _arg_str(args[0]) if args else ""
        pat = _arg_str(args[1]) if len(args) > 1 else "*"
        full = folder_in((ref + pat) if ref else pat)
        for name in self._folder_names():
            if full == "" or full == '""':
                continue
            if self._match_list(full, name):
                disp = utf7_encode(name if name.upper() != "INBOX" else "INBOX")
                self.send(f'* LIST (\\HasNoChildren) "/" {disp}')
        self.ok(tag, "LIST completed")

    def cmd_lsub(self, tag, args):
        self.cmd_list(tag, args)

    def cmd_status(self, tag, args):
        if not self._need_auth(tag):
            return
        name = folder_in(_arg_str(args[0]))
        items = [str(a).upper() for a in args[1:]]
        frow = next((r for r in self.store.list_folders(self.user) if r["name"] == name), None)
        if not frow:
            self.no(tag, "no such folder")
            return
        st = self.store.folder_status(self.user, name)
        kv = []
        for it in items:
            if it == "MESSAGES":
                kv.append(f"MESSAGES {st['messages']}")
            elif it == "UNSEEN":
                kv.append(f"UNSEEN {st['unseen']}")
            elif it == "RECENT":
                kv.append("RECENT 0")
            elif it == "UIDNEXT":
                kv.append(f"UIDNEXT {frow['uidnext']}")
            elif it == "UIDVALIDITY":
                kv.append(f"UIDVALIDITY {frow['uidvalidity']}")
            elif it == "SIZE":
                kv.append(f"SIZE {st['size']}")
        self.send(f"* STATUS {_imap_str(name)} ({' '.join(kv)})")
        self.ok(tag, "STATUS completed")

    def cmd_append(self, tag, args):
        if not self._need_auth(tag):
            return
        name = folder_in(_arg_str(args[0]))
        if name.upper() == "INBOX":
            name = "INBOX"
        flags = ""
        date = ""
        data = None
        for t in args[1:]:
            if isinstance(t, bytes):
                data = t
            elif str(t).startswith("("):
                flags = str(t).strip("()")
            elif re.match(r'".*"|.+', str(t)) and "-" in str(t) and data is None and flags:
                date = str(t)
        if data is None:
            self.fail(tag, "missing message literal")
            return
        if not self.store.quota_ok(self.user, len(data)):
            self.no(tag, "quota exceeded", "[OVERQUOTA]")
            return
        import email as _email
        msg, hdrs, text, _html, atts = _parse(data)
        mid = self.store.deliver(self.user, name, data, flags,
                                 hdrs.get("From", ""), hdrs.get("To", ""),
                                 hdrs.get("Subject", ""), hdrs.get("Date", ""),
                                 text, bool(atts))
        frow = next((r for r in self.store.list_folders(self.user) if r["name"] == name), None)
        uv = frow["uidvalidity"] if frow else 1
        self.ok(tag, f"[APPENDUID {uv} {self.store.get_message(self.user, mid)['uid']}] appended")

    # ---------- 已选中 ----------
    def _exists_update(self):
        self._reload()
        self.send(f"* {len(self.rows)} EXISTS")

    def cmd_check(self, tag, args):
        self._exists_update()
        self.ok(tag, "CHECK completed")

    def cmd_close(self, tag, args):
        if not self._need_auth(tag):
            return
        if not self.ro:
            self.store.expunge_deleted(self.user, self.folder)
        self.folder = None
        self.rows = []
        self.ok(tag, "CLOSE completed")

    def cmd_unselect(self, tag, args):
        self.folder = None
        self.rows = []
        self.ok(tag, "UNSELECT completed")

    def cmd_expunge(self, tag, args):
        if not self._need_auth(tag):
            return
        self._reload()
        gone = [i + 1 for i, r in enumerate(self.rows) if "\\Deleted" in r["flags"]]
        self.store.expunge_deleted(self.user, self.folder)
        for seq in sorted(gone, reverse=True):
            self.send(f"* {seq} EXPUNGE")
        self.ok(tag, "EXPUNGE completed")

    def _apply_flags(self, ids: list[int], op: str, flags: list[str], silent: bool):
        for mid in ids:
            row = self.store.get_message(self.user, mid)
            if not row:
                continue
            cur = self._flag_set(row["flags"])
            imap_flags = {f for f in flags}
            if op == "set":
                new = imap_flags
            elif op == "add":
                new = cur | imap_flags
            else:
                new = cur - imap_flags
            new = {f for f in new if f in ("\\Answered", "\\Flagged", "\\Deleted",
                                           "\\Seen", "\\Draft", "\\Recent")} | \
                  {f for f in new if f.startswith("$")}
            self.store.set_flags(self.user, mid, " ".join(sorted(new)))
            if not silent:
                seq = next((i + 1 for i, r in enumerate(self.rows) if r["id"] == mid), None)
                if seq:
                    self.send(f"* {seq} FETCH (FLAGS ({' '.join(sorted(new))}))")

    def cmd_store(self, tag, args, by_uid=False):
        if not self._need_auth(tag):
            return
        if len(args) < 3:
            self.fail(tag, "STORE needs set, item and flags")
            return
        spec = _arg_str(args[0])
        item = str(args[1]).upper()
        op = "set"
        if item.startswith("+"):
            op, item = "add", item[1:]
        elif item.startswith("-"):
            op, item = "del", item[1:]
        silent = item.endswith(".SILENT")
        item = item.replace(".SILENT", "")
        if item != "FLAGS":
            self.fail(tag, "only FLAGS supported")
            return
        flags = [a.strip("()") for a in args[2:] if isinstance(a, str)]
        flat = []
        for f in flags:
            flat += f.split()
        ids = self._seq_uids(spec, by_uid)
        self._apply_flags(ids, op, flat, silent)
        self.ok(tag, f"{'UID ' if by_uid else ''}STORE completed")

    def cmd_copy(self, tag, args, by_uid=False, move=False):
        if not self._need_auth(tag):
            return
        if len(args) < 2:
            self.fail(tag, "COPY needs set and folder")
            return
        spec = _arg_str(args[0])
        dest = folder_in(_arg_str(args[1]))
        if dest.upper() == "INBOX":
            dest = "INBOX"
        if dest == self.folder:
            self.no(tag, "same folder")
            return
        ids = self._seq_uids(spec, by_uid)
        if not ids:
            self.no(tag, "no such messages")
            return
        copied = []
        for mid in ids:
            new_mid = self.store.copy_message(self.user, mid, dest)
            if new_mid is None:
                self.no(tag, f"copy failed")
                return
            copied.append((mid, new_mid))
        if move:
            for mid, _ in copied:
                row = self.store.get_message(self.user, mid)
                if row:
                    self.store.set_flags(self.user, mid, row["flags"] + " \\Deleted")
            self.store.expunge_deleted(self.user, self.folder)
            self._reload()
        self.ok(tag, f"{'UID ' if by_uid else ''}{'MOVE' if move else 'COPY'} completed")

    # ---------- FETCH ----------
    def cmd_fetch(self, tag, args, by_uid=False):
        if not self._need_auth(tag):
            return
        if len(args) < 2:
            self.fail(tag, "FETCH needs set and items")
            return
        spec = _arg_str(args[0])
        idset = self._seq_uid_set(spec, by_uid)
        self._reload()
        for i, row in enumerate(self.rows):
            want = (row["uid"] if by_uid else i + 1) in idset
            if not want:
                continue
            self._fetch_one(tag, i + 1, row, args[1:], by_uid)
        self.ok(tag, f"{'UID ' if by_uid else ''}FETCH completed")

    def _fetch_one(self, tag, seq, row, items, by_uid):
        parts = []
        seen_touch = False
        toks = list(items)
        i = 0
        while i < len(toks):
            it = str(toks[i]).upper()
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if it == "FLAGS":
                parts.append(f"FLAGS ({row['flags']})")
            elif it == "UID":
                parts.append(f"UID {row['uid']}")
            elif it == "RFC822.SIZE":
                parts.append(f"RFC822.SIZE {row['size']}")
            elif it == "INTERNALDATE":
                parts.append(f'INTERNALDATE "{_imap_date(row["internaldate"])}"')
            elif it == "ENVELOPE":
                msg = email.message_from_bytes(self.store.read_raw(row))
                parts.append(f"ENVELOPE {_envelope(msg)}")
            elif it in ("BODYSTRUCTURE", "BODY"):
                msg = email.message_from_bytes(self.store.read_raw(row))
                parts.append(f"{'BODYSTRUCTURE' if it == 'BODYSTRUCTURE' else 'BODY'} "
                             f"{_bodystructure(msg, ext=it == 'BODYSTRUCTURE')}")
            elif it.startswith("BODY.PEEK[") or it.startswith("BODY["):
                peek = it.startswith("BODY.PEEK[")
                prefix = "BODY.PEEK[" if peek else "BODY["
                sec = it[len(prefix):]           # "BODY[]" → ""；其余为节首段
                if sec.endswith("]"):
                    sec = sec[:-1]
                else:
                    # 拼回被分词的节说明（HEADER.FIELDS (…) 等），直到遇到顶层 ]
                    while i + 1 < len(toks):
                        i += 1
                        t2 = toks[i]
                        if isinstance(t2, str) and t2.endswith("]"):
                            sec += " " + t2[:-1]
                            break
                        sec += " " + (t2 if isinstance(t2, str) else "")
                sec_in = sec.strip()
                partial = ""
                if "<" in sec_in:
                    sec_in, _, partial = sec_in.partition("<")
                raw = self.store.read_raw(row)
                data = _section_data(raw, sec_in)
                label = prefix + sec_in.upper() + "]"
                if partial:
                    try:
                        a, _, b = partial.rstrip(">").partition(".")
                        a = int(a) if a else 0
                        b = int(b) if b else len(data) - a
                        data = data[a:a + b]
                        label += f"<{a}>"
                    except ValueError:
                        pass
                if not peek and not self.ro and "\\Seen" not in row["flags"]:
                    seen_touch = True
                parts.append(f"{label} " + _literal(data))
            elif it == "RFC822":
                raw = self.store.read_raw(row)
                parts.append("RFC822 " + _literal(raw))
                seen_touch = True
            elif it == "RFC822.HEADER":
                raw = self.store.read_raw(row)
                parts.append("RFC822.HEADER " + _literal(_header_block(raw)))
            elif it == "RFC822.TEXT":
                raw = self.store.read_raw(row)
                parts.append("RFC822.TEXT " + _literal(_text_block(raw)))
                seen_touch = True
            i += 1
        if seen_touch:
            newf = self._flag_set(row["flags"]) | {"\\Seen"}
            self.store.set_flags(self.user, row["id"], " ".join(sorted(newf)))
            row = self.store.get_message(self.user, row["id"])
            parts.append(f"FLAGS ({row['flags']})")
        uid_part = f"UID {row['uid']} " if by_uid else ""
        self.send(f"* {seq} FETCH ({uid_part}{' '.join(parts)})")

    # ---------- SEARCH ----------
    def cmd_search(self, tag, args, by_uid=False):
        if not self._need_auth(tag):
            return
        self._reload()
        toks = [t for t in args]
        i = 0
        crits = []
        while i < len(toks):
            fn, i = _search_crit(toks, i, self, by_uid)
            if fn:
                crits.append(fn)
        hits = []
        for idx, row in enumerate(self.rows):
            if all(fn(row) for fn in crits):
                hits.append(row["uid"] if by_uid else idx + 1)
        body = " ".join(str(h) for h in hits)
        self.send(f"* SEARCH{' ' + body if body else ''}")
        self.ok(tag, f"{'UID ' if by_uid else ''}SEARCH completed")

    # ---------- IDLE ----------
    def cmd_idle(self, tag, args):
        if not self._need_auth(tag):
            return
        if not self.folder:
            self.no(tag, "no folder selected")
            return
        self.send("+ idling")
        sock = self.conn.sock
        try:
            sock.settimeout(25)
        except OSError:
            pass
        try:
            while True:
                fired = self.store.wait_change(self.user, self.folder, 20)
                if self.conn.closed:
                    return
                if fired:
                    self._exists_update()
                line = None
                try:
                    line = self.conn.read_line(1024)
                except Exception:
                    pass
                if line and line.strip().upper() == "DONE":
                    self.ok(tag, "IDLE terminated")
                    return
                if line is None:
                    try:
                        self.send("* OK still here")
                    except OSError:
                        return
                elif line == "":
                    return  # 超长/异常，断开
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass

    def cmd_uid(self, tag, args):
        if not args:
            self.fail(tag, "UID needs a command")
            return
        sub = str(args[0]).upper()
        rest = args[1:]
        fn = {"FETCH": self.cmd_fetch, "STORE": self.cmd_store,
              "COPY": self.cmd_copy, "SEARCH": self.cmd_search}.get(sub)
        if fn is None:
            self.fail(tag, f"UID {sub} not supported")
            return
        fn(tag, rest, by_uid=True)

    def cmd_id(self, tag, args):
        self.send('* ID ("name" "SelfMail" "version" "1.0")')
        self.ok(tag, "ID completed")


# ---------- 工具 ----------
def _arg_str(t) -> str:
    if isinstance(t, bytes):
        return t.decode("utf-8", "replace")
    return str(t)


def _tokenize(s: str) -> list:
    toks, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t":
            i += 1
        elif c == '"':
            j, buf = i + 1, ""
            while j < n:
                if s[j] == "\\" and j + 1 < n:
                    buf += s[j + 1]
                    j += 2
                elif s[j] == '"':
                    break
                else:
                    buf += s[j]
                    j += 1
            toks.append(buf)
            i = j + 1
        elif c in "()":
            toks.append(c)
            i += 1
        else:
            j = i
            while j < n and s[j] not in ' \t()"':
                j += 1
            toks.append(s[i:j])
            i = j
    return toks


def _literal(data: bytes) -> str:
    return "{" + str(len(data)) + "}\r\n" + data.decode("utf-8", "surrogateescape")


def _header_block(raw: bytes) -> bytes:
    m = re.search(rb"\r\n\r\n", raw)
    return raw[:m.start() + 2] if m else raw


def _text_block(raw: bytes) -> bytes:
    m = re.search(rb"\r\n\r\n", raw)
    return raw[m.end():] if m else b""


def _parse(raw: bytes):
    from .store import parse_message
    return parse_message(raw)


def _imap_date(internaldate: str) -> str:
    try:
        t = time.strptime(internaldate[:19], "%Y-%m-%d %H:%M:%S")
        return f"{t.tm_mday:02d}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} +0000"
    except Exception:
        return "01-Jan-2026 00:00:00 +0000"


def _section_data(raw: bytes, sec: str) -> bytes:
    if sec == "" or sec.upper() == "TEXT":
        msg = email.message_from_bytes(raw)
        if sec.upper() == "HEADER" or sec == "":
            pass
        if sec.upper() == "TEXT":
            for p in msg.walk():
                if not p.is_multipart() and p.get_content_maintype() == "text":
                    pl = p.get_payload(decode=True)
                    if pl:
                        return pl
            return b""
        return raw
    if sec.upper() == "HEADER":
        return _header_block(raw)
    m = re.match(r"(?i)HEADER\.FIELDS(\.NOT)?\s*\(([^)]*)\)$", sec)
    if m:
        not_mode, names = bool(m.group(1)), [x.strip().upper() for x in m.group(2).split()]
        head = _header_block(raw)
        keep = []
        for ln in head.split(b"\r\n"):
            if not ln:
                continue
            name = ln.split(b":", 1)[0].strip().upper()
            hit = name in [n.encode() for n in names] or name.decode("latin-1") in names
            if (not_mode and not hit) or (not not_mode and hit):
                keep.append(ln)
            elif not not_mode and hit:
                keep.append(ln)
        out = b"\r\n".join(keep)
        if keep and not out.endswith(b"\r\n"):
            out += b"\r\n"
        return out + b"\r\n" if not_mode else out + b"\r\n"
    if re.fullmatch(r"[\d.]+", sec):
        msg = email.message_from_bytes(raw)
        if sec.endswith(".MIME"):
            path = sec[:-5]
            p = _part_by_path(msg, path)
            if p is None:
                return b""
            raw_msg = email.message_from_bytes(raw)
            # MIME 头：从原始 raw 粗略提取该部件头（简化：返回完整头块）
            return _header_block(raw)
        base = sec.split(".")[0]
        p = _part_by_path(msg, sec)
        if p is None:
            return b""
        pl = p.get_payload(decode=True)
        return pl or b""
    return b""


def _search_crit(toks, i, sess, by_uid):
    def s_at(k):
        return _arg_str(toks[k]) if k < len(toks) else ""

    t = str(toks[i]).upper() if i < len(toks) else ""
    if t == "ALL":
        return (lambda r: True), i + 1
    if t in ("SEEN", "UNSEEN", "FLAGGED", "UNFLAGGED", "DELETED", "UNDELETED",
             "ANSWERED", "UNANSWERED", "DRAFT", "UNDRAFT"):
        want = t.startswith("UN")
        canon = {"SEEN": "\\Seen", "FLAGGED": "\\Flagged", "DELETED": "\\Deleted",
                 "ANSWERED": "\\Answered", "DRAFT": "\\Draft"}[t[2:] if want else t]
        return (lambda r, f=canon, w=want:
                (f in (r["flags"] or "").split()) != w), i + 1
    if t == "RECENT":
        return (lambda r: False), i + 1
    if t == "OLD":
        return (lambda r: True), i + 1
    if t == "NEW":
        return (lambda r: False), i + 1
    if t == "CHARSET":
        return None, i + 2
    if t in ("FROM", "TO", "CC", "BCC", "SUBJECT", "BODY", "TEXT"):
        val = s_at(i + 1).lower()
        if t in ("FROM", "TO", "CC", "BCC", "SUBJECT"):
            key = {"FROM": "msg_from", "TO": "msg_to", "SUBJECT": "subject"}.get(t, None)

            def fn(r, key=key, val=val):
                if key == "msg_from":
                    return val in (r["msg_from"] or "").lower()
                if key == "msg_to":
                    return val in (r["msg_to"] or "").lower()
                if key == "subject":
                    return val in (r["subject"] or "").lower()
                raw = sess.store.read_raw(r).decode("utf-8", "replace").lower()
                return val in raw
            return fn, i + 2
        raw_need = True

        def fn(r, val=val):
            raw = sess.store.read_raw(r).decode("utf-8", "replace").lower()
            return val in raw
        return fn, i + 2
    if t == "HEADER":
        fname = s_at(i + 1).lower()
        val = s_at(i + 2).lower()

        def fn(r, fname=fname, val=val):
            raw = sess.store.read_raw(r)
            head = _header_block(raw).decode("utf-8", "replace").lower()
            if not val:
                return fname.encode() in head
            lines = [l for l in head.split("\r\n") if l.lower().startswith(fname + ":")]
            return any(val in l.lower() for l in lines)
        return fn, i + 3
    if t in ("SINCE", "BEFORE", "ON"):
        import datetime as _dt
        d = _parse_date(s_at(i + 1))

        def fn(r, d=d, mode=t):
            try:
                idate = _dt.datetime.strptime(r["internaldate"][:19], "%Y-%m-%d %H:%M:%S").date()
            except Exception:
                return False
            if not d:
                return False
            if mode == "SINCE":
                return idate >= d
            if mode == "BEFORE":
                return idate < d
            return idate == d
        return fn, i + 2
    if t in ("SENTSINCE", "SENTBEFORE", "SENTON"):
        import datetime as _dt
        d = _parse_date(s_at(i + 1))

        def fn(r, d=d, mode=t):
            try:
                m = email.utils.parsedate_to_datetime(r["date"])
                mdate = m.date()
            except Exception:
                return False
            if not d:
                return False
            if mode == "SENTSINCE":
                return mdate >= d
            if mode == "SENTBEFORE":
                return mdate < d
            return mdate == d
        return fn, i + 2
    if t in ("LARGER", "SMALLER"):
        n = int(s_at(i + 1) or 0)

        def fn(r, n=n, big=t == "LARGER"):
            return r["size"] > n if big else r["size"] < n
        return fn, i + 2
    if t == "UID":
        spec = s_at(i + 1)
        uids = set(_seq_uids_static(spec, sess.rows, True))

        def fn(r, uids=uids):
            return r["uid"] in uids
        return fn, i + 2
    if t == "NOT":
        fn, ni = _search_crit(toks, i + 1, sess, by_uid)
        return (lambda r, fn=fn: not fn(r)), ni
    if t == "OR":
        f1, i1 = _search_crit(toks, i + 1, sess, by_uid)
        f2, i2 = _search_crit(toks, i1, sess, by_uid)
        return (lambda r, f1=f1, f2=f2: f1(r) or f2(r)), i2
    if t == "(":
        inner = []
        j = i + 1
        depth = 1
        while j < len(toks) and depth:
            if toks[j] == "(":
                depth += 1
            elif toks[j] == ")":
                depth -= 1
                if not depth:
                    break
            fn, j = _search_crit(toks, j, sess, by_uid)
            if fn:
                inner.append(fn)
            else:
                j += 1

        def fn(r, inner=inner):
            return all(g(r) for g in inner)
        return fn, j + 1
    if re.fullmatch(r"[\d:,*]+", t):
        seqs = set(_seq_uids_static(t, sess.rows, False))

        def fn(r, seqs=seqs):
            return True  # 已在行级过滤
        # 行级：直接判定本行序号/uid
        idx = next((k for k, rr in enumerate(sess.rows) if rr["id"] == r_id(r)), None)
        return (lambda r: True), i + 1
    return None, i + 1


def r_id(r):
    return r["id"]


def _seq_uids_static(spec: str, rows, by_uid: bool) -> list[int]:
    if not rows:
        return []
    out = []
    total = len(rows)
    for part in spec.split(","):
        if ":" in part:
            a, _, b = part.partition(":")
            lo = total if a in ("*", "") else int(a or 1)
            hi = total if b in ("*", "") else int(b or total)
            if lo > hi:
                lo, hi = hi, lo
        else:
            lo = hi = total if part in ("*", "") else int(part or 0)
        for n in range(max(1, lo), min(hi, total) + 1):
            row = rows[n - 1]
            out.append(row["uid"] if by_uid else n)
    return out


def _parse_date(s: str):
    import datetime as _dt
    try:
        d, _, y = s.partition("-")
        return _dt.date(int(y), _MONTHS.index(d[2:].title()) + 1, int(d[:2]))
    except Exception:
        return None


def start_listeners(app, tls_ctx):
    stops = []
    cfg = app.cfg
    if cfg.imap.get("enable", True):
        if cfg.imap.get("port_tls", 993):
            stops.append(serve_forever("0.0.0.0", int(cfg.imap["port_tls"]),
                                       lambda c: IMAPSession(app, c).run(),
                                       "imap-993", tls_ctx=tls_ctx, tls_only=True))
        if cfg.imap.get("port", 143):
            stops.append(serve_forever("0.0.0.0", int(cfg.imap["port"]),
                                       lambda c: IMAPSession(app, c).run(),
                                       "imap-143"))
    return stops
