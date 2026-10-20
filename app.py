"""Local control panel: python app.py (also runs on Android Python/Termux)."""
import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import subprocess
import threading
import time
import urllib.parse
import uuid
import webbrowser

from whale import Whale, atomic, image_type, text
from social import Social

ROOT = Path(__file__).resolve().parent


def inline_image(encoded):
    raw=base64.b64decode(text(encoded,6_666_680),validate=True)
    mime=image_type(raw)
    if not mime or len(raw)>5_000_000:raise ValueError("图片须为5MB以内的PNG、JPEG、GIF或WebP")
    return mime,raw


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Android/Linux must rebind after a crash despite TIME_WAIT; Windows needs exclusive binding.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # ponytail: numeric loopback needs no reverse DNS; VMOS aborts during getfqdn.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # No chat text, credentials, URLs or request bodies in access logs.

    def answer(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        expected = "127.0.0.1:" + str(self.server.server_port)
        if self.headers.get("Host") != expected:
            self.answer(403, {"error": "仅允许本机访问"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + expected:
            self.answer(403, {"error": "拒绝跨站请求"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.answer(403, {"error": "拒绝跨站请求"})
            return False
        return True

    def authorized(self):
        if not self.trusted():
            return False
        token = self.server.social.qq.token if self.path=="/bridge/qq/event" else self.server.social.token if self.path.startswith("/bridge/") else self.server.token
        if not secrets.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
            self.answer(401, {"error": "访问链接已失效，请使用终端显示的本机链接"})
            return False
        return True

    def do_GET(self):
        self.connection.settimeout(10)
        if self.path == "/":
            if self.trusted():
                self.answer(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/vendor/ai-companion/humanize.js":
            if self.trusted():
                self.answer(200,(ROOT/"vendor/ai-companion/humanize.js").read_bytes(),"text/javascript; charset=utf-8")
        elif self.path == "/api/state":
            if self.authorized():
                self.answer(200, self.server.whale.snapshot())
        elif self.path == "/health":
            if self.trusted():
                self.answer(200, {"service": "LittleWhale", "started": self.server.started,
                                  "heartbeat": self.server.whale.setting("heartbeat_at", self.server.started)})
        elif self.path == "/api/role":
            if self.authorized():
                self.answer(200, {"role": (self.server.whale.home / "role.md").read_text("utf-8")})
        elif self.path == "/api/social":
            if self.authorized():
                self.answer(200, self.server.social.snapshot())
        elif self.path == "/api/bridge-pair":
            if self.authorized():
                self.answer(200, {"token": self.server.social.token})
        elif self.path.startswith("/album/"):
            if not self.authorized():
                return
            filename = self.path.removeprefix("/album/")
            with self.server.whale.lock:
                found = self.server.whale.db.execute("SELECT file FROM album WHERE file=?", (filename,)).fetchone()
            if found:
                path = self.server.whale.home / "album" / found["file"]
                types = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
                self.answer(200, path.read_bytes(), types.get(path.suffix, "application/octet-stream"))
            else:
                self.answer(404, {"error": "图片不存在"})
        else:
            self.answer(404, {"error": "页面不存在"})

    def do_POST(self):
        self.connection.settimeout(10)
        qq_event=self.path=="/bridge/qq/event"
        if not (self.trusted() if qq_event else self.authorized()):
            self.close_connection = True
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请求格式错误或内容超限")
            raw=self.read_body(20000 if qq_event else 7_000_000,chunked=qq_event)
            size=len(raw)
            if qq_event:
                signature="sha1="+hmac.new(self.server.social.qq.token.encode(),raw,hashlib.sha1).hexdigest()
                if not secrets.compare_digest(self.headers.get("X-Signature","").encode(),signature.encode()):
                    self.server.whale.set_setting("qq_ingress",dict(at=time.time(),http=401))
                    self.answer(401,{"error":"QQ事件签名无效"});self.close_connection=True;return
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("请求须为JSON对象")
            whale = self.server.whale
            if self.path.startswith("/bridge/"):
                if size > 20000:
                    raise ValueError("消息内容超限")
                methods = {"/bridge/qq/event":self.server.social.qq.receive,"/bridge/event": self.server.social.receive, "/bridge/claim": self.server.social.claim,
                           "/bridge/ack": self.server.social.acknowledge, "/bridge/status": self.server.social.report,
                           "/bridge/pending": self.server.social.poll}
                if self.path not in methods:
                    raise ValueError("不存在的接入操作")
                result = methods[self.path](body)
                if qq_event:self.server.whale.set_setting("qq_ingress",dict(at=time.time(),http=202,status=result.get("status")))
            elif self.path == "/api/social":
                result = self.server.social.configure(body)
            elif self.path == "/api/qq-pair":
                if body:raise ValueError("QQ配对不接受账号参数")
                result=self.server.social.qq.pair()
            elif self.path == "/api/qq-owner":
                result=self.server.social.qq.authorize_owner(body)
            elif self.path == '/api/qq-features':
                result=self.server.social.qq.save_features(body)
            elif self.path=="/api/qq-diagnose":
                if body:raise ValueError("QQ诊断不接受消息参数")
                with whale.lock:
                    targets=whale.db.execute("SELECT t.id FROM social_threads t JOIN social_qq q ON q.thread=t.id WHERE t.enabled=1 AND t.person='owner' AND q.kind='private' AND q.bot=?",(whale.setting("qq_bot"),)).fetchall()
                if len(targets)!=1:raise ValueError("请先明确唯一的QQ本人私聊")
                result=whale.test_connection(thread="qq:"+targets[0][0])
            elif self.path == "/api/social-policy":
                result = self.server.social.save_policy(body)
            elif self.path == "/api/chat":
                images = []
                if body.get("image"):
                    images.append(inline_image(body["image"]))
                result = whale.chat(body.get("message"), body.get("event_id"), images=images)
            elif self.path == "/api/config":
                result = whale.save_config(body)
            elif self.path == "/api/test-model":
                if set(body)-{"image"}:raise ValueError("连接检查不接受消息或其他参数")
                result = whale.test_connection(images=[inline_image(body["image"])] if "image" in body else None)
            elif self.path == "/api/pause":
                if type(body.get("paused")) is not bool:
                    raise ValueError("暂停参数错误")
                whale.set_setting("paused", body["paused"])
                result = {"paused": body["paused"]}
            elif self.path == "/api/role":
                role = text(body.get("role"), 12000)
                with whale.lock:
                    atomic(whale.home / "role.md", role.encode())
                result = {"saved": True}
            elif self.path == "/api/name":
                name = text(body.get("name"), 60)
                with whale.lock, whale.db:
                    whale.db.execute("UPDATE people SET name=? WHERE id='owner'", (name,))
                result = {"saved": True}
            elif self.path == "/api/memory":
                key = text(body.get("key"), 60)
                with whale.lock, whale.db:
                    if body.get("delete") is True:
                        whale.db.execute("DELETE FROM memory WHERE person='owner' AND key=?", (key,))
                    else:
                        value = text(body.get("value"), 400)
                        whale.db.execute("INSERT OR REPLACE INTO memory VALUES('owner',?,?,?)", (key, value, whale.clock()))
                result = {"saved": True}
            elif self.path == "/api/album":
                encoded = text(body.get("image"), 6_666_680)
                result = {"hash": whale.collect(base64.b64decode(encoded, validate=True), body.get("reason"),sticker=body.get("sticker",False))}
            else:
                self.answer(404, {"error": "功能不存在"})
                return
            self.answer(202 if self.path in ("/bridge/event","/bridge/qq/event") else 200, result)
        except (ValueError, KeyError, TypeError, TimeoutError) as error:
            safe = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "输入格式不正确或请求超时"
            if qq_event:self.server.whale.set_setting("qq_ingress",dict(at=time.time(),http=400,error=safe[:100]))
            self.answer(400, {"error": safe[:300]})
            self.close_connection = True
        except Exception:
            self.answer(500, {"error": "操作失败，已保留已有数据；请检查存储和服务状态"})
            self.close_connection = True

    def read_body(self,maximum,*,chunked=False):
        lengths=self.headers.get_all("Content-Length",[])
        encodings=self.headers.get_all("Transfer-Encoding",[])
        if encodings:
            if not chunked or lengths or len(encodings)!=1 or encodings[0].lower()!="chunked":
                raise ValueError("请求传输格式不支持")
            raw=bytearray()
            # ponytail: NapCat's Node HTTP client sends plain chunks without trailers; reject trailers and extensions.
            for _ in range(1024):
                line=self.rfile.readline(32)
                if not re.fullmatch(rb"[0-9a-fA-F]{1,16}\r\n",line):raise ValueError("QQ分块格式无效")
                size=int(line[:-2],16)
                if len(raw)+size>maximum:raise ValueError("消息内容超限")
                if not size:
                    if self.rfile.read(2)!=b"\r\n" or not raw:raise ValueError("QQ分块结束格式无效")
                    return bytes(raw)
                data=self.rfile.read(size)
                if len(data)!=size or self.rfile.read(2)!=b"\r\n":raise ValueError("QQ分块未完整送达")
                raw.extend(data)
            raise ValueError("QQ分块数量超限")
        if len(lengths)!=1 or not lengths[0].isascii() or not lengths[0].isdigit():raise ValueError("请求长度无效")
        size=int(lengths[0])
        if not 0<size<=maximum:raise ValueError("请求格式错误或内容超限")
        raw=self.rfile.read(size)
        if len(raw)!=size:raise ValueError("请求未完整送达")
        return raw


def acquire(home):
    home.mkdir(parents=True, exist_ok=True)
    handle = (home / "process.lock").open("a+b")
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def main():
    parser = argparse.ArgumentParser(description="小鲸鱼 · 本机运行")
    parser.add_argument("--home", type=Path, default=Path.home() / ".littlewhale")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口须为1024至65535")
    try:
        singleton = acquire(args.home)
    except OSError:
        parser.error("此数据目录已在运行，请打开已有页面")
    with singleton, Server(("127.0.0.1", args.port), Handler) as server:
        server.whale = Whale(args.home)
        server.social = Social(server.whale)
        try:
            subprocess.run(("termux-wake-lock",),timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        except (OSError,subprocess.SubprocessError):
            pass
        server.started = time.time()
        server.token = secrets.token_urlsafe(32)
        url = f"http://127.0.0.1:{server.server_port}/#" + server.token
        atomic(server.whale.home / "panel-url", url.encode())
        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(30):
                try:
                    server.whale.set_setting("heartbeat_at", time.time())
                    server.social.tick()
                    server.whale.set_setting("heartbeat_error", False)
                except Exception:
                    server.whale.set_setting("heartbeat_error", True)
                try:
                    server.whale.sync_notebooks()
                except Exception:
                    server.whale.set_setting("notebook_status",dict(at=time.time(),ok=False))

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        social_thread=threading.Thread(target=server.social.work,args=(stopped,),daemon=True)
        social_thread.start()
        print("小鲸鱼本机页面（链接只留在自己设备上）：\n" + url, flush=True)
        print("真实聊天需要模型密钥；QQ/微信还需通知助手、账号登录及会话授权。", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
            server.social.ready.set()
            deadline=time.monotonic()+65
            thread.join(max(0,deadline-time.monotonic()))
            social_thread.join(max(0,deadline-time.monotonic()))
            # Daemon HTTP requests may still be unwinding; close the DB only when no model call is active.
            if not thread.is_alive() and not social_thread.is_alive() and server.whale.behavior.acquire(timeout=max(0,deadline-time.monotonic())):
                server.whale.close()
                server.whale.behavior.release()


if __name__ == "__main__":
    main()
