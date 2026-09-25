import json
import logging
import os
import sys
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Dict, Any

from .config import config
from .auth import session_store, verify_admin_password, verify_client_token
from .publisher import Publisher, PublisherError
from .rate_limiter import download_concurrency_limiter, ip_rate_limiter
from .scanner import compute_file_sha256, ScannerError, DirectoryUnavailableError
from .generator import get_available_templates, generate_configured_jar, GeneratorError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("mc-publisher")

publisher = Publisher(config.MODS_DIR, config.STATE_DIR)

TEMPLATES_DIR = Path(__file__).parent / "templates"

class PublisherHTTPHandler(BaseHTTPRequestHandler):
    server_version = "MCPublisher/1.0"

    def log_message(self, format, *args):
        # Clean logging format
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def send_json(self, status_code: int, data: Dict[str, Any], set_cookie: Optional[str] = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status_code: int, html_str: str, set_cookie: Optional[str] = None):
        body = html_str.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, set_cookie: Optional[str] = None):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def get_cookie(self, name: str) -> Optional[str]:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            if name in cookie:
                return cookie[name].value
        except Exception:
            return None
        return None

    def get_current_session(self) -> Optional[Dict[str, Any]]:
        token = self.get_cookie("mc_admin_session")
        if not token:
            return None
        return session_store.get_session(token)

    def read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw_body = self.rfile.read(content_length)
        try:
            return json.loads(raw_body.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. Health check (no leak of credentials or paths)
        if path == "/healthz":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return

        # 2. Latest manifest for client mod
        if path == "/api/v1/latest":
            self.handle_api_latest()
            return

        # 3. File download for client mod
        if path.startswith("/api/v1/files/"):
            file_id = path[len("/api/v1/files/"):]
            self.handle_api_download(file_id)
            return

        # 4. Admin API: status
        if path == "/api/admin/status":
            self.handle_admin_status()
            return

        # 4.1 Admin API: templates
        if path == "/api/admin/templates":
            self.handle_admin_templates()
            return

        # 5. Web UI: Login page
        if path == "/login":
            self.render_login_page()
            return

        # 6. Web UI: Admin dashboard
        if path == "/" or path == "/index.html":
            sess = self.get_current_session()
            if not sess:
                self.redirect("/login")
                return
            self.render_dashboard_page()
            return

        # Not found
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND", "message": "接口不存在"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/admin/login":
            self.handle_admin_login()
            return

        if path == "/api/admin/logout":
            self.handle_admin_logout()
            return

        if path == "/api/admin/scan":
            self.handle_admin_scan()
            return

        if path == "/api/admin/publish":
            self.handle_admin_publish()
            return

        if path == "/api/admin/toggle-download":
            self.handle_admin_toggle_download()
            return

        if path == "/api/admin/generate-updater":
            self.handle_admin_generate_updater()
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND", "message": "接口不存在"})

    # --- Client API Handlers ---

    def handle_api_latest(self):
        # 1. Authenticate client token
        auth_header = self.headers.get("Authorization")
        valid, err_code, err_msg = verify_client_token(auth_header)
        if not valid:
            status = HTTPStatus.UNAUTHORIZED if err_code == "UNAUTHORIZED" else HTTPStatus.FORBIDDEN
            self.send_json(status, {"error": err_code, "message": err_msg})
            return

        # 2. Check service status
        if not publisher.is_download_enabled():
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "SERVICE_DISABLED",
                "message": "客户端更新与下载服务当前已停用维护中"
            })
            return

        # 3. Get manifest
        manifest = publisher.get_manifest()
        if not manifest:
            self.send_json(HTTPStatus.NOT_FOUND, {
                "error": "NOT_PUBLISHED",
                "message": "尚未发布任何整合包版本"
            })
            return

        self.send_json(HTTPStatus.OK, manifest)

    def handle_api_download(self, file_id: str):
        # 1. Rate limiter check
        client_ip = self.client_address[0]
        if not ip_rate_limiter.is_allowed(client_ip):
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {
                "error": "RATE_LIMIT_EXCEEDED",
                "message": "请求频率过高，请稍后重试"
            })
            return

        # 2. Authenticate client token
        auth_header = self.headers.get("Authorization")
        valid, err_code, err_msg = verify_client_token(auth_header)
        if not valid:
            status = HTTPStatus.UNAUTHORIZED if err_code == "UNAUTHORIZED" else HTTPStatus.FORBIDDEN
            self.send_json(status, {"error": err_code, "message": err_msg})
            return

        # 3. Check service status
        if not publisher.is_download_enabled():
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "SERVICE_DISABLED",
                "message": "客户端更新与下载服务当前已停用维护中"
            })
            return

        # 4. Check manifest
        manifest = publisher.get_manifest()
        if not manifest:
            self.send_json(HTTPStatus.NOT_FOUND, {
                "error": "NOT_PUBLISHED",
                "message": "尚未发布任何整合包版本"
            })
            return

        # 5. Look up file_id in published manifest
        file_entry = next((f for f in manifest.get("files", []) if f["id"] == file_id), None)
        if not file_entry:
            self.send_json(HTTPStatus.NOT_FOUND, {
                "error": "FILE_NOT_FOUND",
                "message": "请求的文件未包含在当前已发布清单中"
            })
            return

        filename = file_entry["filename"]
        target_path = (config.MODS_DIR / filename).resolve()

        # Security check: must reside inside MODS_DIR and not be a symlink
        try:
            if not target_path.is_relative_to(config.MODS_DIR) or target_path.is_symlink():
                self.send_json(HTTPStatus.FORBIDDEN, {
                    "error": "FORBIDDEN",
                    "message": "非法的文件路径访问"
                })
                return
        except AttributeError:
            # Python < 3.9 fallback if any
            if os.path.relpath(target_path, config.MODS_DIR).startswith(".."):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "FORBIDDEN", "message": "非法路径"})
                return

        # Check if file exists on disk
        if not target_path.is_file():
            self.send_json(HTTPStatus.GONE, {
                "error": "FILE_DELETED_ON_DISK",
                "message": "源文件在发布后已被移除，请等待管理员重新发布"
            })
            return

        # Consistency check: verify that current file SHA-256 matches the published hash
        try:
            current_hash, current_size = compute_file_sha256(target_path)
        except Exception as e:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "FILE_READ_ERROR",
                "message": f"校验源文件完整性失败: {e}"
            })
            return

        if current_hash != file_entry["sha256"]:
            self.send_json(HTTPStatus.CONFLICT, {
                "error": "FILE_INTEGRITY_MISMATCH",
                "message": "源文件在发布后已被修改，拒绝提供不一致的下载，请等待管理员重新发布"
            })
            return

        # 6. Concurrency limiter
        if not download_concurrency_limiter.acquire(timeout=2.0):
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {
                "error": "CONCURRENCY_LIMIT_EXCEEDED",
                "message": "服务器下载并发数已达上限，请稍后重试"
            })
            return

        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/java-archive")
            self.send_header("Content-Length", str(current_size))

            # RFC 5987 / URL encoded filename
            encoded_fn = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f'attachment; filename="{encoded_fn}"; filename*=UTF-8\'\'{encoded_fn}')
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()

            with open(target_path, "rb") as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            download_concurrency_limiter.release()

    # --- Admin API Handlers ---

    def _require_admin(self) -> Optional[Dict[str, Any]]:
        sess = self.get_current_session()
        if not sess:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "UNAUTHORIZED", "message": "请先登录"})
            return None

        # Verify CSRF for state-modifying requests
        csrf_header = self.headers.get("X-CSRF-Token")
        if not csrf_header or csrf_header != sess.get("csrf_token"):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "CSRF_ERROR", "message": "CSRF 安全校验未通过"})
            return None

        return sess

    def handle_admin_login(self):
        body = self.read_json_body()
        password = body.get("password", "")

        if not verify_admin_password(password):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"success": False, "message": "密码错误"})
            return

        token, csrf_token = session_store.create_session()
        max_age = config.SESSION_TTL_HOURS * 3600
        cookie_header = f"mc_admin_session={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax"

        self.send_json(HTTPStatus.OK, {
            "success": True,
            "csrf_token": csrf_token,
            "message": "登录成功"
        }, set_cookie=cookie_header)

    def handle_admin_logout(self):
        token = self.get_cookie("mc_admin_session")
        if token:
            session_store.delete_session(token)
        cookie_header = "mc_admin_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        self.send_json(HTTPStatus.OK, {"success": True, "message": "已登出"}, set_cookie=cookie_header)

    def handle_admin_status(self):
        sess = self.get_current_session()
        if not sess:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "UNAUTHORIZED", "message": "未登录"})
            return

        try:
            files, dir_stats = scan_mods_directory(config.MODS_DIR)
        except Exception:
            dir_stats = {"total_count": 0, "total_size": 0, "scan_digest": ""}

        manifest = publisher.get_manifest()
        state = publisher.get_state()

        self.send_json(HTTPStatus.OK, {
            "success": True,
            "csrf_token": sess["csrf_token"],
            "manifest": manifest,
            "download_enabled": publisher.is_download_enabled(),
            "last_user_notes": state.get("last_user_notes", ""),
            "dir_stats": dir_stats
        })

    def handle_admin_scan(self):
        if not self._require_admin():
            return

        try:
            preview_data = publisher.preview_changes()
            self.send_json(HTTPStatus.OK, preview_data)
        except DirectoryUnavailableError as e:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "DIRECTORY_UNAVAILABLE",
                "message": str(e)
            })
        except ScannerError as e:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "SCAN_ERROR",
                "message": str(e)
            })

    def handle_admin_publish(self):
        if not self._require_admin():
            return

        body = self.read_json_body()
        notes = body.get("notes", "")
        scan_digest = body.get("scan_digest", "")
        allow_empty = bool(body.get("allow_empty", False))

        try:
            new_manifest = publisher.publish(notes, scan_digest, allow_empty=allow_empty)
            self.send_json(HTTPStatus.OK, {
                "success": True,
                "manifest": new_manifest,
                "message": "发布成功"
            })
        except PublisherError as e:
            self.send_json(HTTPStatus.BAD_REQUEST, {
                "error": e.code,
                "code": e.code,
                "message": e.message,
                "data": e.data
            })
        except Exception as e:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "INTERNAL_ERROR",
                "message": f"发布过程出现异常: {e}"
            })

    def handle_admin_toggle_download(self):
        if not self._require_admin():
            return

        body = self.read_json_body()
        enabled = bool(body.get("enabled", True))
        publisher.set_download_enabled(enabled)
        self.send_json(HTTPStatus.OK, {
            "success": True,
            "download_enabled": enabled,
            "message": f"下载服务已{'启用' if enabled else '停用'}"
        })

    def handle_admin_templates(self):
        sess = self.get_current_session()
        if not sess:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "UNAUTHORIZED", "message": "未登录"})
            return

        templates = get_available_templates()
        host = self.headers.get("Host", "").split(":")[0] or "127.0.0.1"
        configured_endpoint = getattr(config, "CLIENT_ENDPOINT", "") or getattr(config, "DEFAULT_CLIENT_ENDPOINT", "")
        default_endpoint = configured_endpoint or f"http://{host}:{config.PORT}"

        self.send_json(HTTPStatus.OK, {
            "success": True,
            "templates": templates,
            "default_endpoint": default_endpoint,
            "pack_id": config.PACK_ID
        })

    def handle_admin_generate_updater(self):
        if not self._require_admin():
            return

        body = self.read_json_body()
        template_id = body.get("template_id", "")
        client_endpoint = body.get("client_endpoint", "").strip()

        try:
            jar_bytes, filename = generate_configured_jar(
                template_id=template_id,
                endpoint=client_endpoint,
                pack_id=config.PACK_ID,
                token=config.CLIENT_TOKEN
            )

            encoded_fn = urllib.parse.quote(filename)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/java-archive")
            self.send_header("Content-Length", str(len(jar_bytes)))
            self.send_header("Content-Disposition", f'attachment; filename="{encoded_fn}"; filename*=UTF-8\'\'{encoded_fn}')
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(jar_bytes)
        except GeneratorError as e:
            self.send_json(HTTPStatus.BAD_REQUEST, {
                "error": e.code,
                "message": e.message
            })
        except Exception as e:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "GENERATION_FAILED",
                "message": f"生成客户端更新器失败: {e}"
            })

    # --- HTML Rendering ---

    def render_login_page(self):
        path = TEMPLATES_DIR / "login.html"
        with open(path, "r", encoding="utf-8") as f:
            template = f.read()
        rendered = template.replace("{{ APP_NAME }}", config.APP_NAME)
        self.send_html(HTTPStatus.OK, rendered)

    def render_dashboard_page(self):
        path = TEMPLATES_DIR / "index.html"
        with open(path, "r", encoding="utf-8") as f:
            template = f.read()
        rendered = template.replace("{{ APP_NAME }}", config.APP_NAME)
        rendered = rendered.replace("{{ PACK_ID }}", config.PACK_ID)
        rendered = rendered.replace("{{ HOST_MODS_DIR }}", config.HOST_MODS_DIR)
        self.send_html(HTTPStatus.OK, rendered)

def run():
    # Preflight check on directories
    config.MODS_DIR.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)

    server_address = ("0.0.0.0", config.PORT)
    httpd = ThreadingHTTPServer(server_address, PublisherHTTPHandler)
    logger.info("==================================================")
    logger.info("  %s 启动就绪", config.APP_NAME)
    logger.info("  监听端口: %s", config.PORT)
    logger.info("  Mod发布目录: %s", config.MODS_DIR)
    logger.info("  状态持久化目录: %s", config.STATE_DIR)
    logger.info("==================================================")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务收到中断信号，正在退出...")
        httpd.server_close()

if __name__ == "__main__":
    run()
