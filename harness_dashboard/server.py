#!/usr/bin/env python3
"""Local, read-only browser for harness session data."""

import argparse
import ctypes
import ctypes.util
import datetime
import errno
import json
import mimetypes
import os
import secrets
import subprocess
import threading
import webbrowser
from types import SimpleNamespace
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import db_adapters, file_adapters, remote_adapter, source_files

STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
HOME = Path.home()
ASSETS = {"/app.js", "/styles.css", "/theme.js"}


def public(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


def file_reference(file):
    """Create a macOS file-ID URL from the already validated open descriptor."""
    library = ctypes.util.find_library("CoreFoundation")
    if not library:
        raise OSError("Системные файловые ссылки недоступны")
    cf = ctypes.CDLL(library)
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFURLCreateFileReferenceURL.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFURLCreateFileReferenceURL.restype = ctypes.c_void_p
    cf.CFURLGetString.argtypes = [ctypes.c_void_p]
    cf.CFURLGetString.restype = ctypes.c_void_p
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    path = f"/dev/fd/{file.fileno()}".encode()
    url = cf.CFURLCreateFromFileSystemRepresentation(None, path, len(path), False)
    if not url:
        raise OSError("Не удалось создать ссылку на файл")
    try:
        reference = cf.CFURLCreateFileReferenceURL(None, url, None)
        if not reference:
            raise OSError("Файловая система не поддерживает безопасную ссылку")
        try:
            buffer = ctypes.create_string_buffer(4096)
            if not cf.CFStringGetCString(cf.CFURLGetString(reference), buffer, len(buffer), 0x08000100):
                raise OSError("Не удалось прочитать файловую ссылку")
            result = buffer.value.decode("utf-8")
            if not result.startswith("file:///.file/id="):
                raise OSError("Получена неустойчивая файловая ссылка")
            return result
        finally:
            cf.CFRelease(reference)
    finally:
        cf.CFRelease(url)


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, remote_config=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.remote_config = remote_config
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.sessions = {}
        self.sources = []
        self.roots = {}
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()

    def scan(self):
        with self.scan_lock:
            sessions = []
            sources = []
            for adapter in (file_adapters, db_adapters, remote_adapter):
                try:
                    found, status = (adapter.scan(HOME, config=self.remote_config) if adapter is remote_adapter
                                     else adapter.scan(HOME))
                    sessions.extend(found)
                    sources.extend([status] if isinstance(status, dict) else status)
                except Exception as exc:
                    sources.append({"name": adapter.__name__, "status": "error", "detail": str(exc), "root": ""})
            roots = {}
            for source in sources:
                if source.get("name") == "Hermes VPS":
                    continue
                if not source.get("root"):
                    continue
                try:
                    root = Path(source["root"])
                    descriptor = file_adapters._open_root(root)
                    try:
                        info = os.fstat(descriptor)
                        roots[source["name"]] = (root, (info.st_dev, info.st_ino))
                    finally:
                        os.close(descriptor)
                except (OSError, ValueError):
                    continue
            for item in sessions:
                root_info = roots.get(item.get("source"))
                if root_info and (not item.get("_root_identity") or tuple(item["_root_identity"]) == root_info[1]):
                    item["_reveal_root"], item["_reveal_identity"] = str(root_info[0]), root_info[1]
            with self.lock:
                self.sessions = {item["id"]: item for item in sessions}
                self.sources = sources
                self.roots = roots
            return {"sessions": [public(item) for item in sessions], "sources": sources,
                    "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    def lookup(self, session_id):
        with self.lock:
            return self.sessions.get(session_id)


class Handler(BaseHTTPRequestHandler):
    server: Dashboard

    def log_message(self, _format, *_args):
        pass

    def headers_for(self, status, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Harness-Dashboard", "1")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()

    def respond(self, status, body, content_type="text/plain; charset=utf-8"):
        self.headers_for(status, content_type)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_response(self, status, value):
        self.respond(status, json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def allowed_host(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        return (host == f"127.0.0.1:{self.server.server_port}"
                and (origin is None or origin == self.server.origin)
                and (site is None or site in ("same-origin", "none")))

    def authorized(self):
        token = self.headers.get("X-Harness-Token", "")
        return secrets.compare_digest(token, self.server.token)

    def do_GET(self):
        if not self.allowed_host():
            self.json_response(403, {"error": "Forbidden origin"})
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
            html = html.replace('<script src="/bootstrap.js">', f'<meta name="harness-token" content="{self.server.token}">\n  <script src="/bootstrap.js">', 1)
            self.respond(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif parsed.path == "/bootstrap.js":
            self.respond(200, b'window.HARNESS_TOKEN = document.querySelector("meta[name=harness-token]")?.content || "";', "text/javascript; charset=utf-8")
        elif parsed.path in ASSETS:
            path = STATIC_ROOT / parsed.path[1:]
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.respond(200, path.read_bytes(), content_type + "; charset=utf-8")
        elif parsed.path.startswith("/api/"):
            if not self.authorized():
                self.json_response(403, {"error": "Forbidden"})
                return
            if parsed.path == "/api/sessions":
                self.json_response(200, self.server.scan())
            elif parsed.path == "/api/session":
                params = parse_qs(parsed.query, keep_blank_values=True)
                session_id = params.get("id", [""])[0]
                item = self.server.lookup(session_id)
                if not item:
                    self.json_response(404, {"error": "Сессия не найдена. Обновите список."})
                    return
                try:
                    offset = int(params.get("offset", ["0"])[0])
                    if offset < 0:
                        raise ValueError("Недопустимая позиция")
                    adapter = (remote_adapter if item.get("_adapter") == "remote" else
                               file_adapters if item.get("_adapter") == "file" or item["source"] in ("OMP", "DSH", "Claude") else db_adapters)
                    result = (adapter.detail(item, offset=offset, config=self.server.remote_config)
                              if adapter is remote_adapter else adapter.detail(item, offset=offset))
                    with self.server.lock:
                        parent = self.server.sessions.get(item.get("parent_id", ""))
                        root_info = self.server.roots.get(item["source"])
                        if root_info:
                            for linked_file in result.get("files", []):
                                try:
                                    relative = Path(linked_file["path"]).relative_to(root_info[0])
                                    if relative.parts and source_files._preview_allowed(item["source"], relative.parts):
                                        linked_file["relative"] = relative.as_posix()
                                except (KeyError, TypeError, ValueError):
                                    continue
                        children = [public(row) for row in self.server.sessions.values() if row.get("parent_id") == session_id]
                    result.update({"session": public(item), "parent": public(parent) if parent else None, "children": children})
                    self.json_response(200, result)
                except Exception as exc:
                    self.json_response(500, {"error": f"Не удалось прочитать сессию: {exc}"})
            elif parsed.path in ("/api/files", "/api/file"):
                params = parse_qs(parsed.query, keep_blank_values=True)
                source = params.get("source", [""])[0]
                relative = params.get("path", [""])[0]
                try:
                    offset = int(params.get("offset", ["0"])[0])
                    if offset < 0:
                        raise ValueError("Недопустимая позиция")
                    if source == "Hermes VPS":
                        result = (remote_adapter.remote_files(relative, offset=offset, config=self.server.remote_config) if parsed.path == "/api/files"
                                  else remote_adapter.remote_file(relative, offset=offset, config=self.server.remote_config))
                    else:
                        with self.server.lock:
                            root = self.server.roots.get(source)
                        if not root:
                            raise ValueError("Хранилище недоступно. Обновите источники.")
                        result = (source_files.list_files(*root, relative, offset, source=source) if parsed.path == "/api/files"
                                  else source_files.read_file(*root, relative, offset, source=source))
                    result["source"] = source
                    self.json_response(200, result)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.json_response(400, {"error": str(exc)})
            else:
                self.json_response(404, {"error": "Not found"})
        else:
            self.respond(404, b"Not found")

    def do_POST(self):
        if not self.allowed_host() or self.headers.get("Origin") != self.server.origin or not self.authorized():
            self.json_response(403, {"error": "Forbidden"})
            return
        if urlsplit(self.path).path != "/api/reveal":
            self.json_response(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096 or self.headers.get_content_type() != "application/json":
                raise ValueError("Некорректный запрос")
            body = json.loads(self.rfile.read(length))
            if body.get("id"):
                item = self.server.lookup(body["id"])
                if not item or not item.get("path"):
                    self.json_response(404, {"error": "Файл сессии не найден"})
                    return
                if not item.get("_reveal_root") or not item.get("_reveal_identity"):
                    raise ValueError("Хранилище изменилось или недоступно; обновите список")
                root = Path(item["_reveal_root"])
                target = Path(item["path"])
                with file_adapters._open_source(target, root, item["_reveal_identity"], item.get("_file_identity")) as source:
                    reference = file_reference(source)
                    subprocess.run(["open", "-R", "-u", reference], check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                name, relative = body.get("source", ""), body.get("path", "")
                with self.server.lock:
                    root_info = self.server.roots.get(name)
                if not root_info:
                    raise ValueError("Локальное хранилище недоступно")
                try:
                    fd = source_files.open_directory(*root_info, relative)
                except NotADirectoryError:
                    fd = source_files.open_file(*root_info, relative, source=name)
                try:
                    reference = file_reference(SimpleNamespace(fileno=lambda: fd))
                    subprocess.run(["open", "-R", "-u", reference], check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                finally:
                    os.close(fd)
            self.json_response(200, {"ok": True})
        except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            self.json_response(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="Локальная панель сессий харнессов")
    parser.add_argument("--port", type=int, default=8765, help="локальный порт (0 — выбрать автоматически)")
    parser.add_argument("--open", action="store_true", help="открыть панель в браузере при запуске")
    parser.add_argument("--hermes-config", help="путь к локальному JSON с настройками SSH и корня Hermes")
    args = parser.parse_args()
    try:
        config = remote_adapter.load_config(args.hermes_config) if args.hermes_config else None
    except ValueError as exc:
        parser.error(str(exc))
    try:
        server = Dashboard(args.port, remote_config=config)
    except OSError as exc:
        if not args.open or exc.errno != errno.EADDRINUSE:
            raise
        if args.hermes_config:
            parser.error("Порт занят; перед запуском с настройками Hermes остановите существующий сервер.")
        connection = HTTPConnection("127.0.0.1", args.port, timeout=2)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            if response.status != 200 or response.getheader("X-Harness-Dashboard") != "1":
                parser.error("Порт занят другим приложением или старой версией панели; освободите порт или выберите --port.")
        except (OSError, HTTPException) as error:
            parser.error(f"Не удалось проверить приложение на занятом порту: {error}")
        finally:
            connection.close()
        origin = f"http://127.0.0.1:{args.port}/"
        print(f"Панель уже запущена: {origin}", flush=True)
        webbrowser.open(origin)
        return
    print(f"Панель: {server.origin}/", flush=True)
    try:
        if args.open:
            webbrowser.open(f"{server.origin}/")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
