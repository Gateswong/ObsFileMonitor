"""本地 Web 配置界面（仅使用标准库 http.server）。

接口一览（详见 docs/ARCHITECTURE.md）：

=======  ================  ==========================================
方法      路径               说明
=======  ================  ==========================================
GET      /                 配置页面（web/index.html）
GET      /api/config       当前配置；密码不回传，只返回是否已设置
POST     /api/config       保存配置（JSON），校验失败返回 400
GET      /api/status       运行状态快照
POST     /api/test         用提交的 OBS 连接参数测试连接
POST     /api/quit         退出整个程序
=======  ================  ==========================================

安全措施：只监听 127.0.0.1；校验 Host 头以防 DNS 重绑定；POST 必须是
``application/json``（浏览器跨站提交表单无法携带该类型而不触发 CORS 预检，
而本服务不响应预检），以防其他网页偷偷修改配置。
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import ConfigError, ConfigStore
from .obs_client import test_connection
from .state import SharedState

log = logging.getLogger(__name__)

INDEX_HTML = Path(__file__).with_name("web") / "index.html"
MAX_BODY = 64 * 1024


def _public_config(cfg: dict) -> dict:
    """去掉明文密码后的配置，用于返回给浏览器。"""
    cfg["obs"]["password_set"] = bool(cfg["obs"]["password"])
    cfg["obs"]["password"] = None
    return cfg


def _merge_password(submitted_obs: dict, stored_obs: dict) -> dict:
    """浏览器提交 password=None 表示“保持原密码不变”。"""
    obs = dict(submitted_obs)
    obs.pop("password_set", None)
    if obs.get("password") is None:
        obs["password"] = stored_obs["password"]
    return obs


class WebServer(threading.Thread):
    def __init__(self, store: ConfigStore, state: SharedState, quit_event: threading.Event) -> None:
        super().__init__(name="WebServer", daemon=True)
        self.store = store
        self.state = state
        self.quit_event = quit_event
        self.host, self.port = "127.0.0.1", store.get()["web"]["port"]
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def run(self) -> None:
        log.info("配置页面：%s", self.url)
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()

    def _make_handler(self):
        server = self
        allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 默认会把每个请求打印到 stderr
                log.debug("%s - %s", self.address_string(), fmt % args)

            # ---------- 工具方法 ----------

            def _send_json(self, data, status=HTTPStatus.OK):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status, message):
                self._send_json({"error": message}, status)

            def _host_ok(self) -> bool:
                if self.headers.get("Host", "") in allowed_hosts:
                    return True
                self._error(HTTPStatus.FORBIDDEN, "Host 不被允许")
                return False

            def _read_json(self):
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "需要 application/json")
                    return None
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY:
                    self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大")
                    return None
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._error(HTTPStatus.BAD_REQUEST, "JSON 格式错误")
                    return None

            # ---------- 路由 ----------

            def do_GET(self):
                if not self._host_ok():
                    return
                if self.path == "/":
                    body = INDEX_HTML.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/config":
                    self._send_json(_public_config(server.store.get()))
                elif self.path == "/api/status":
                    self._send_json(server.state.snapshot())
                else:
                    self._error(HTTPStatus.NOT_FOUND, "Not Found")

            def do_POST(self):
                if not self._host_ok():
                    return
                data = self._read_json()
                if data is None:
                    return
                stored = server.store.get()
                if self.path == "/api/config":
                    if not isinstance(data.get("obs"), dict):
                        return self._error(HTTPStatus.BAD_REQUEST, "缺少 obs 配置")
                    data["obs"] = _merge_password(data["obs"], stored["obs"])
                    try:
                        saved = server.store.update(data)
                    except ConfigError as e:
                        return self._error(HTTPStatus.BAD_REQUEST, str(e))
                    result = {"config": _public_config(saved)}
                    if saved["web"] != stored["web"]:
                        result["notice"] = "Web 配置页面端口已保存，重启程序后生效"
                    self._send_json(result)
                elif self.path == "/api/test":
                    obs = _merge_password(data, stored["obs"])
                    ok, message = test_connection(obs)
                    self._send_json({"ok": ok, "message": message})
                elif self.path == "/api/quit":
                    self._send_json({"ok": True})
                    server.quit_event.set()
                else:
                    self._error(HTTPStatus.NOT_FOUND, "Not Found")

        return Handler
