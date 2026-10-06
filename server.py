"""Token-scoped read access and selection submission; no filesystem routes."""
import json
import gzip
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent

def public_submission(s):
    return {k: s[k] for k in ('id', 'items', 'created')}

class AlbumServer:
    def __init__(self, project, port=17860):
        self.project = project
        outer = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def send_bytes(self, data, status=200, mime='application/json; charset=utf-8'):
                compressed = len(data) > 1024 and 'gzip' in self.headers.get('Accept-Encoding', '')
                if compressed:
                    data = gzip.compress(data, compresslevel=5)
                self.send_response(status)
                self.send_header('Vary', 'Accept-Encoding')
                if compressed:
                    self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)

            def reply(self, value, status=200):
                self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), status)

            def route(self):
                parts = urlsplit(self.path).path.strip('/').split('/')
                if len(parts) < 2 or parts[0] != 'a':
                    raise ValueError('无效链接')
                token = parts[1]
                with project.lock:
                    gid = project.authenticate(token)
                return token, gid, parts[2:]

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                try:
                    token, gid, route = self.route()
                    with project.lock:
                        group = project.data['groups'][gid]
                        allowed = {i['id']: i for i in project.data['items'] if gid in i['groups'] and i['reviewed'] and not i['error']}
                        if not route:
                            self.send_bytes((ROOT / 'web/album.html').read_bytes(), mime='text/html; charset=utf-8')
                            return
                        if route == ['state']:
                            items = [{'id': i['id'], 'name': i['name'], 'kind': i['kind'],
                                      'revision': f'{i["size"]:x}-{i["mtime"]:x}-p2'} for i in allowed.values()]
                            submissions = [s for s in project.data['submissions'] if s['group'] == gid and set(s['items']) <= allowed.keys()]
                            self.reply({'title': project.data['title'], 'name': group['name'], 'items': items,
                                        'latest': public_submission(submissions[-1]) if submissions else None})
                            return
                        if len(route) == 3 and route[0] == 'media' and route[1] in allowed:
                            item = allowed[route[1]]
                            suffix = {'thumb': '.webp', 'view': '.jpg', 'video': '.mp4'}.get(route[2])
                            if suffix is None or (route[2] == 'video' and item['kind'] != 'video'):
                                raise ValueError('无效素材')
                            path = project.cache / (item['id'] + suffix)
                            attachment = None
                        else:
                            raise ValueError('无效链接')
                    self.file(path, attachment)
                except (ValueError, FileNotFoundError):
                    self.reply({'error': '链接无效、素材未就绪或相册已停止发布'}, 404)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def file(self, path, attachment):
                with path.open('rb') as source:
                    size = path.stat().st_size
                    start, end, status = 0, size - 1, 200
                    ranges = self.headers.get('Range')
                    if ranges:
                        match = re.fullmatch(r'bytes=(\d*)-(\d*)', ranges)
                        if not match or not any(match.groups()):
                            self.send_range_error(size)
                            return
                        a, b = match.groups()
                        if a:
                            start = int(a)
                            end = min(size - 1, int(b)) if b else size - 1
                        else:
                            start = max(0, size - int(b))
                        if start >= size or end < start:
                            self.send_range_error(size)
                            return
                        status = 206
                    length = max(0, end - start + 1)
                    self.send_response(status)
                    self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
                    self.send_header('Content-Length', str(length))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Cache-Control', 'private, max-age=86400' if not attachment else 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    if status == 206:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    if attachment:
                        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(attachment))
                    self.end_headers()
                    if self.command == 'HEAD':
                        return
                    source.seek(start)
                    while length:
                        block = source.read(min(length, 128 * 1024))
                        if not block:
                            break
                        self.wfile.write(block)
                        length -= len(block)

            def send_range_error(self, size):
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_POST(self):
                try:
                    token, _, route = self.route()
                    if route != ['submit']:
                        raise ValueError('无效请求')
                    # JSON requests only; cross-origin browser requests are not enabled.
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise ValueError('请求格式不正确')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1024 * 1024:
                        raise ValueError('请求过大或为空')
                    self.connection.settimeout(15)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError('请求格式不正确')
                    result = project.submit(token, payload.get('ids'), payload.get('requestId'))
                    self.reply(public_submission(result), 202)
                except (ValueError, TypeError, KeyError) as exc:
                    self.close_connection = True
                    self.reply({'error': str(exc)}, 400)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    self.close_connection = True

        self.http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.http.daemon_threads = True
        self.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
