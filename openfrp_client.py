"""Manage one OpenFrp process owned by this application."""
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / '.openfrp-settings.json'


def validate_settings(value):
    exe = Path(value.get('executable', '')).expanduser().resolve()
    config = Path(value.get('config', '')).expanduser().resolve()
    url = value.get('url', '').strip().rstrip('/')
    parsed = urlsplit(url)
    if not exe.is_file() or exe.suffix.lower() != '.exe':
        raise ValueError('请选择解压后的 OpenFrp frpc 可执行文件（不是启动器）')
    if not config.is_file() or config.suffix.lower() not in ('.ini', '.toml', '.json', '.yaml', '.yml'):
        raise ValueError('请选择从 OpenFrp 控制台获取的隧道配置文件')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('公网入口请填写完整 HTTP 或 HTTPS 地址，不包含相册路径')
    try:
        parsed.port
    except ValueError:
        raise ValueError('公网入口端口无效')
    return dict(executable=str(exe), config=str(config), url=url)


class OpenFrpClient:
    def __init__(self):
        self.process = None
        self.events = queue.Queue()
        self.settings = {}
        if SETTINGS.exists():
            try:
                self.settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                pass

    def save(self, value):
        if self.running:
            raise ValueError('请先停止 OpenFrp，再修改配置')
        self.settings = validate_settings(value)
        temp = SETTINGS.with_suffix('.tmp')
        temp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(SETTINGS)

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running:
            return False
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        value = validate_settings(self.settings)
        self.process = subprocess.Popen([value['executable'], '--config', value['config']],
            cwd=str(Path(value['executable']).parent), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        process = self.process
        def probe():
            # The root intentionally returns 404; it contains no album token.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def response(url):
                try:
                    result = opener.open(url, timeout=3)
                except urllib.error.HTTPError as exc:
                    result = exc
                with result:
                    return result.status, result.read(4096)
            for _ in range(10):
                if self.process is not process or process.poll() is not None:
                    return
                try:
                    local = response('http://127.0.0.1:17860/')
                    public = response(value['url'] + '/')
                    if local == public and b'error' in public[1]:
                        self.events.put('公网入口已响应；请用手机流量验证完整相册链接')
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(2)
            if self.process is process and process.poll() is None:
                self.events.put('客户端运行中，但公网入口暂未验证成功；请检查节点与配置')
        threading.Thread(target=probe, daemon=True).start()
        def read():
            # Do not persist raw output: client logs may contain account secrets.
            try:
                for raw in iter(process.stdout.readline, b''):
                    line = raw.decode('utf-8', errors='replace').lower()
                    if 'start proxy success' in line:
                        self.events.put('隧道已建立；请用异地手机验证访问')
                    elif 'login to server success' in line:
                        self.events.put('已连接节点，正在建立隧道')
                    elif any(t in line for t in ('error', 'failed', 'conflict')):
                        self.events.put('连接异常：请检查配置、节点状态或隧道是否重复启动')
                code = process.wait()
                if self.process is process:
                    self.events.put(f'OpenFrp 已退出（代码 {code}）')
            finally:
                process.stdout.close()
        threading.Thread(target=read, daemon=True).start()
        return True

    def stop(self):
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self.events.put('OpenFrp 已停止')
