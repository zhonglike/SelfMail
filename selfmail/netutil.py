"""线程化网络基础层：行协议连接封装（支持中途 STARTTLS 升级）+ 监听器。
小团队规模下每连接一线程最稳健；TLS 用标准 ssl.SSLContext。
"""
from __future__ import annotations

import socket
import ssl
import threading
import traceback

MAX_LINE = 8192


class LineConn:
    """提供 read_line / read_exact / send / starttls；自动处理 \r\n。"""

    def __init__(self, sock: socket.socket, addr, tls: bool = False):
        self.addr = addr
        self.tls = tls
        self.sock = sock
        self._rfile = sock.makefile("rb")
        self.closed = False

    def read_line(self, limit: int = MAX_LINE) -> str | None:
        try:
            line = self._rfile.readline(limit + 1)
        except (OSError, ssl.SSLError, ValueError):
            return None
        if not line:
            return None
        if len(line) > limit:  # 超长：丢弃剩余并返回截断内容
            try:
                while True:
                    extra = self._rfile.readline(MAX_LINE)
                    if not extra or extra.endswith(b"\n"):
                        break
            except (OSError, ssl.SSLError, ValueError):
                return None
            return line[:limit].decode("utf-8", "replace").rstrip("\r\n")
        return line.decode("utf-8", "replace").rstrip("\r\n")

    def read_exact(self, n: int) -> bytes | None:
        try:
            data = self._rfile.read(n)
        except (OSError, ssl.SSLError, ValueError):
            return None
        return data if data is not None and len(data) == n else None

    def send(self, text: str):
        try:
            self.sock.sendall(text.encode("utf-8", "surrogateescape") + b"\r\n")
        except (OSError, ssl.SSLError):
            self.closed = True

    def send_raw(self, data: bytes):
        try:
            self.sock.sendall(data)
        except (OSError, ssl.SSLError):
            self.closed = True

    def starttls(self, ctx: ssl.SSLContext) -> bool:
        """中途升级为 TLS。升级前客户端不得再发送明文数据。"""
        try:
            self._rfile.close()
        except OSError:
            pass
        try:
            self.sock = ctx.wrap_socket(self.sock, server_side=True)
            self.tls = True
            self._rfile = self.sock.makefile("rb")
            return True
        except (ssl.SSLError, OSError):
            self.closed = True
            return False

    def peer_ip(self) -> str:
        try:
            return self.addr[0]
        except Exception:
            return ""

    def close(self):
        self.closed = True
        for closer in (self._rfile, self.sock):
            try:
                closer.close()
            except OSError:
                pass


def serve_forever(host: str, port: int, handler, name: str,
                  tls_ctx: ssl.SSLContext | None = None, tls_only=False):
    """起监听线程；每连接新线程调用 handler(conn)。返回 (server_thread, stop_event)。"""
    stop = threading.Event()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    srv.settimeout(1.0)

    def accept_loop():
        while not stop.is_set():
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            sock.settimeout(300)

            def work(s=sock, a=addr):
                conn = None
                try:
                    if tls_ctx and tls_only:
                        s = tls_ctx.wrap_socket(s, server_side=True)
                        conn = LineConn(s, a, tls=True)
                    else:
                        conn = LineConn(s, a)
                    handler(conn)
                except Exception:
                    traceback.print_exc()
                finally:
                    if conn:
                        conn.close()
                    else:
                        try:
                            s.close()
                        except OSError:
                            pass

            threading.Thread(target=work, daemon=True, name=f"{name}-conn").start()

    t = threading.Thread(target=accept_loop, daemon=True, name=f"{name}-listen")
    t.start()

    def stop_fn():
        stop.set()
        try:
            srv.close()
        except OSError:
            pass

    return stop_fn
