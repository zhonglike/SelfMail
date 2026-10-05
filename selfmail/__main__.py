"""SelfMail 命令行入口：
  python -m selfmail serve                  启动全部服务
  python -m selfmail init                   生成配置 + 管理员账号
  python -m selfmail adduser tom s3cret     建账号（--admin --quota 2048）
  python -m selfmail passwd tom s3cret2     改密码
  python -m selfmail list / deluser tom     查看 / 删除
  python -m selfmail alias add sales tom    别名 -> 本地账号
  python -m selfmail gen-cert               重新生成自签名证书
  python -m selfmail gen-dns                输出需要在域名商处配置的 DNS 记录
  python -m selfmail check-dns              实时校验 DNS 记录是否生效
"""
from __future__ import annotations

import argparse
import getpass
import sys
import threading
import time

from . import __version__
from .auth import hash_password


def _cfg(args):
    from . import config
    return config.load(getattr(args, "config", None))


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="config.json 路径（默认 data/config.json）")
    p = argparse.ArgumentParser(prog="selfmail", description="SelfMail 自邮 · 自托管邮件服务器",
                                parents=[common])
    p.add_argument("--version", action="version", version=f"SelfMail {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="启动全部服务", parents=[common])
    sub.add_parser("init", help="初始化配置与管理员账号", parents=[common])
    pa = sub.add_parser("adduser", parents=[common]); pa.add_argument("user"); pa.add_argument("password", nargs="?")
    pa.add_argument("--admin", action="store_true"); pa.add_argument("--quota", type=int, default=2048)
    pa.add_argument("--name", default="")
    pd = sub.add_parser("deluser", parents=[common]); pd.add_argument("user")
    pp = sub.add_parser("passwd", parents=[common]); pp.add_argument("user"); pp.add_argument("password", nargs="?")
    pl = sub.add_parser("list", aliases=["users"], parents=[common])
    pa2 = sub.add_parser("alias", parents=[common]); pa2.add_argument("action", choices=["add", "del", "list"])
    pa2.add_argument("alias", nargs="?"); pa2.add_argument("target", nargs="?")
    sub.add_parser("gen-cert", parents=[common])
    sub.add_parser("gen-dns", parents=[common])
    sub.add_parser("check-dns", parents=[common])
    sub.add_parser("version")

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "version":
        print(__version__)
        return 0

    cfg = _cfg(args)

    if cmd == "serve":
        return cmd_serve(cfg)

    from .app import App
    app = App(cfg)
    store = app.store

    if cmd == "init":
        print(f"配置文件：{cfg.path}")
        print(f"邮件域名：{cfg.domain}    主机名：{cfg.hostname}")
        admin = input("管理员账号（默认 admin）: ").strip() or "admin"
        pw = getpass.getpass("管理员密码（至少 8 位）: ") or ""
        if len(pw) < 8:
            print("密码太短"); return 1
        ok = store.create_user(admin, pw, "管理员", True, 0)
        print("创建成功" if ok else "账号已存在")
        print("\n下一步：python -m selfmail serve  → 浏览器打开 https://localhost")
        return 0

    if cmd == "adduser":
        pw = args.password or getpass.getpass(f"{args.user} 的密码: ")
        ok = store.create_user(args.user.lower(), pw, args.name, args.admin, args.quota)
        print(f"{args.user}@{cfg.domain} 创建成功" if ok else "创建失败（已存在？）")
        return 0 if ok else 1

    if cmd == "deluser":
        store.delete_user(args.user.lower())
        print("已删除")
        return 0

    if cmd == "passwd":
        pw = args.password or getpass.getpass(f"{args.user} 的新密码: ")
        if len(pw) < 8:
            print("密码至少 8 位"); return 1
        store.set_password(args.user.lower(), pw)
        print("已修改")
        return 0

    if cmd in ("list", "users"):
        print(f"{'账号':<20}{'管理员':<6}{'配额MB':<8}{'已用MB':<9}{'创建时间'}")
        for r in store.list_users():
            print(f"{r['username']:<20}{'是' if r['is_admin'] else '-':<6}"
                  f"{r['quota_mb'] or '∞':<8}{r['used'] // 1048576:<9}{r['created']}")
        return 0

    if cmd == "alias":
        if args.action == "list":
            for r in store.list_aliases():
                print(f"{r['alias']} -> {r['target']}")
            return 0
        if args.action == "add":
            a = args.alias or ""
            t = args.target or ""
            if "@" not in a:
                a = f"{a}@{cfg.domain}"
            ok = store.add_alias(a, t)
            print("别名已添加" if ok else "添加失败")
            return 0 if ok else 1
        store.del_alias((args.alias or "").lower())
        print("已删除")
        return 0

    if cmd == "gen-cert":
        from pathlib import Path
        from .tlsutil import generate_self_signed
        generate_self_signed(Path(cfg.cert_path), Path(cfg.key_path),
                             cfg.hostname, cfg.domain)
        print(f"证书已生成：{cfg.cert_path}")
        return 0

    if cmd == "gen-dns":
        from .dkim import ensure_keys
        pub = ensure_keys(cfg)
        d, sel, host = cfg.domain, cfg.dkim_selector, cfg.hostname
        from .dnssetup import detect_public_ip
        print(f"=== 请在域名解析商处添加以下记录（{d}）===")
        rows = [
            ("A", host, detect_public_ip(), "服务器地址（已自动检测）"),
            ("MX", f"@ {d}", f"10 {host}", "收信路由"),
            ("TXT", f"@ {d}", "v=spf1 mx a -all", "SPF"),
            ("TXT", f"{sel}._domainkey.{d}", f"v=DKIM1; k=rsa; p={pub}", "DKIM（>255字符需分段加引号）"),
            ("TXT", f"_dmarc.{d}", f"v=DMARC1; p=quarantine; rua=mailto:postmaster@{d}", "DMARC"),
        ]
        for t, h, v, note in rows:
            print(f"\n[{t}]  {h}\n  {v}\n  # {note}")
        print("\n提示：家宽用户请用 scripts/ddns_cloudflare.py 自动更新 A 记录。")
        return 0

    if cmd == "check-dns":
        from . import dnsres
        d, host = cfg.domain, cfg.hostname
        print(f"检查 {d} 的 DNS 记录（实时）…")
        mx = dnsres.resolve_mx(d)
        print(f"MX  : {mx or '未找到 ✗'}  期望 10 {host}")
        spf = dnsres.find_txt_record(d, "v=spf1")
        print(f"SPF : {spf or '未找到 ✗'}")
        dkim = dnsres.find_txt_record(f"{cfg.dkim_selector}._domainkey.{d}", "v=DKIM1")
        print(f"DKIM: {'已找到 ✓' if dkim else '未找到 ✗'}")
        dmarc = dnsres.find_txt_record(f"_dmarc.{d}", "v=DMARC1")
        print(f"DMARC: {dmarc or '未找到 ✗'}")
        a = dnsres.resolve(host, "A")
        print(f"A   : {host} -> {a or '未找到 ✗'}")
        print("\n端口 25 出站连通性测试（对 Gmail MX）…")
        try:
            import socket as s
            mx_host = mx[0][1] if mx else "gmail-smtp-in.l.google.com"
            with socket.create_connection((mx_host, 25), timeout=8):
                print(f"✓ 可连通 {mx_host}:25 —— 出站端口未被封")
        except Exception as e:  # noqa: BLE001
            print(f"✗ 无法直连 25 端口（{e}）—— ISP 封锁，请在配置中启用 relay 中继模式")
        return 0

    return 0


def cmd_serve(cfg) -> int:
    from .app import App
    from . import smtp_server, imap_server, pop3_server, webserver
    from .tlsutil import ensure_cert, make_context
    from .dkim import ensure_keys

    app = App(cfg)
    ensure_cert(cfg)
    app.tls_ctx = make_context(cfg.cert_path, cfg.key_path)
    ensure_keys(cfg)

    stops = []
    stops += smtp_server.start_listeners(app, app.tls_ctx)
    stops += imap_server.start_listeners(app, app.tls_ctx)
    stops += pop3_server.start_listeners(app, app.tls_ctx)
    webserver.serve(app)
    threading.Thread(target=smtp_server.run_outbound_worker, args=(app,),
                     daemon=True, name="outbound").start()

    print(f"\n  SelfMail 自邮 v{__version__} 已启动")
    print(f"  域名     : {cfg.domain}（主机名 {cfg.hostname}）")
    print(f"  Webmail  : https://<本机>:{cfg.web['port']}")
    print(f"  收发端口 : SMTP {cfg.smtp['port_in']}/{cfg.smtp['port_tls']}/{cfg.smtp['port_submit']}"
          f" | IMAP {cfg.imap['port']}/{cfg.imap['port_tls']}"
          f" | POP3 {cfg.pop3['port']}/{cfg.pop3['port_tls']}")
    print(f"  出站模式 : {cfg.outbound['mode']}")
    print("  Ctrl+C 停止\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n正在停止…")
        for f in stops:
            try:
                f()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
