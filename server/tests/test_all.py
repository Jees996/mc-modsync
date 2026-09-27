import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.cookies import SimpleCookie
from pathlib import Path

# Set test environment before importing app modules
test_tmp = tempfile.mkdtemp(prefix="mc_pub_test_")
test_mods_dir = Path(test_tmp) / "mods"
test_state_dir = Path(test_tmp) / "state"
test_mods_dir.mkdir(parents=True)
test_state_dir.mkdir(parents=True)

os.environ["PORT"] = "29999"
os.environ["PACK_ID"] = "test-pack"
os.environ["APP_NAME"] = "测试模组发布器"
os.environ["HOST_MODS_DIR"] = str(test_mods_dir)
os.environ["MODS_DIR"] = str(test_mods_dir)
os.environ["STATE_DIR"] = str(test_state_dir)
os.environ["ADMIN_PASSWORD"] = "test_admin_pass_123"
os.environ["CLIENT_TOKEN"] = "test_client_token_abc"

from app.config import config
# Re-assign config paths in case of pre-init
config.PORT = 29999
config.MODS_DIR = test_mods_dir
config.STATE_DIR = test_state_dir
config.ADMIN_PASSWORD = "test_admin_pass_123"
config.CLIENT_TOKEN = "test_client_token_abc"

from app.main import ThreadingHTTPServer, PublisherHTTPHandler, publisher

class PublisherEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_address = ("127.0.0.1", 29999)
        cls.httpd = ThreadingHTTPServer(cls.server_address, PublisherHTTPHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(test_tmp, ignore_errors=True)

    def setUp(self):
        # Clean up files in mods and state
        for p in test_mods_dir.iterdir():
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
        for p in test_state_dir.iterdir():
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
        publisher._init_state_if_needed()

    def _http_request(self, method: str, path: str, data: dict = None, headers: dict = None, cookie: str = None):
        url = f"http://127.0.0.1:29999{path}"
        req_headers = headers or {}
        if cookie:
            req_headers["Cookie"] = cookie

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read()
                resp_headers = resp.info()
                return resp.status, resp_headers, resp_body
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def _admin_login(self):
        status, headers, body = self._http_request("POST", "/api/admin/login", {"password": "test_admin_pass_123"})
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertTrue(data["success"])
        csrf_token = data["csrf_token"]

        cookie_str = headers.get("Set-Cookie")
        cookie = SimpleCookie()
        cookie.load(cookie_str)
        session_cookie = f"mc_admin_session={cookie['mc_admin_session'].value}"
        return session_cookie, csrf_token

    def test_01_healthz(self):
        """1. Health check returns 200 without leaking credentials"""
        status, _, body = self._http_request("GET", "/healthz")
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data.get("status"), "ok")
        self.assertNotIn("password", body.decode().lower())
        self.assertNotIn("token", body.decode().lower())

    def test_02_unauthenticated_client_access(self):
        """2. Client API without token or with wrong token is rejected"""
        # No token
        status, _, body = self._http_request("GET", "/api/v1/latest")
        self.assertEqual(status, 401)
        data = json.loads(body.decode())
        self.assertEqual(data.get("error"), "UNAUTHORIZED")

        # Wrong token
        status, _, body = self._http_request("GET", "/api/v1/latest", headers={"Authorization": "Bearer wrong_token"})
        self.assertEqual(status, 403)
        data = json.loads(body.decode())
        self.assertEqual(data.get("error"), "FORBIDDEN")

    def test_03_first_release_and_scan(self):
        """3. First release: all files recognized as added, notes appended"""
        cookie, csrf = self._admin_login()

        # Place 2 initial jars
        mod_a = test_mods_dir / "mod-alpha.jar"
        mod_b = test_mods_dir / "mod-beta.jar"
        mod_a.write_bytes(b"ALPHA_CONTENT_V1")
        mod_b.write_bytes(b"BETA_CONTENT_V1")

        # Admin scan
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(len(data["changes"]["added"]), 2)
        self.assertIn("mod-alpha.jar", data["changes"]["added"])
        self.assertIn("mod-beta.jar", data["changes"]["added"])
        scan_digest = data["stats"]["scan_digest"]

        # Admin publish
        user_notes = "本次发布初始整合包测试"
        status, _, body = self._http_request(
            "POST", "/api/admin/publish",
            data={"notes": user_notes, "scan_digest": scan_digest},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        pub_data = json.loads(body.decode())
        manifest = pub_data["manifest"]
        self.assertEqual(len(manifest["files"]), 2)
        self.assertIn("本次发布初始整合包测试", manifest["notes"])
        self.assertIn("Mod文件变化：", manifest["notes"])
        self.assertIn("+ mod-alpha.jar", manifest["notes"])
        self.assertIn("+ mod-beta.jar", manifest["notes"])

        # Client queries latest
        status, _, body = self._http_request("GET", "/api/v1/latest", headers={"Authorization": "Bearer test_client_token_abc"})
        self.assertEqual(status, 200)
        client_manifest = json.loads(body.decode())
        self.assertEqual(client_manifest["release_id"], manifest["release_id"])
        self.assertEqual(len(client_manifest["files"]), 2)

    def test_04_add_new_file(self):
        """4. Add a new file is detected as added"""
        cookie, csrf = self._admin_login()
        # Setup initial
        (test_mods_dir / "mod-a.jar").write_bytes(b"MOD_A")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        self._http_request("POST", "/api/admin/publish", data={"notes": "v1", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)

        # Add new file
        (test_mods_dir / "mod-b.jar").write_bytes(b"MOD_B")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        scan_data = json.loads(body.decode())
        self.assertEqual(scan_data["changes"]["added"], ["mod-b.jar"])
        self.assertEqual(scan_data["changes"]["modified"], [])
        self.assertEqual(scan_data["changes"]["deleted"], [])

    def test_05_modified_same_name_same_size_detected_by_sha256(self):
        """5. File modified with same size and name is detected as modified by SHA-256"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-fixed-size.jar").write_bytes(b"12345678")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        self._http_request("POST", "/api/admin/publish", data={"notes": "v1", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)

        # Overwrite with different content of exact same size
        (test_mods_dir / "mod-fixed-size.jar").write_bytes(b"87654321")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        scan_data = json.loads(body.decode())
        self.assertEqual(scan_data["changes"]["modified"], ["mod-fixed-size.jar"])
        self.assertEqual(scan_data["changes"]["added"], [])
        self.assertEqual(scan_data["changes"]["deleted"], [])

    def test_06_delete_file(self):
        """6. Deleting a file is detected as deleted"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-to-delete.jar").write_bytes(b"DELETE_ME")
        (test_mods_dir / "mod-keep.jar").write_bytes(b"KEEP_ME")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        self._http_request("POST", "/api/admin/publish", data={"notes": "v1", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)

        # Delete file
        (test_mods_dir / "mod-to-delete.jar").unlink()
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        scan_data = json.loads(body.decode())
        self.assertEqual(scan_data["changes"]["deleted"], ["mod-to-delete.jar"])
        self.assertIn("mod-keep.jar", scan_data["changes"]["unchanged"])

    def test_07_rename_file_shows_as_delete_and_add(self):
        """7. Renaming a file is treated as deleted old + added new"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-old.jar").write_bytes(b"SAME_CONTENT")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        self._http_request("POST", "/api/admin/publish", data={"notes": "v1", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)

        # Rename
        (test_mods_dir / "mod-old.jar").rename(test_mods_dir / "mod-new.jar")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        scan_data = json.loads(body.decode())
        self.assertIn("mod-old.jar", scan_data["changes"]["deleted"])
        self.assertIn("mod-new.jar", scan_data["changes"]["added"])

    def test_08_update_notes_only(self):
        """8. Updating notes without file changes is permitted"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-static.jar").write_bytes(b"STATIC_CONTENT")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        self._http_request("POST", "/api/admin/publish", data={"notes": "第一次公告", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)

        # Publish new notes without file changes
        status, _, body = self._http_request(
            "POST", "/api/admin/publish",
            data={"notes": "修改后的新公告", "scan_digest": digest},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        manifest = data["manifest"]
        self.assertIn("修改后的新公告", manifest["notes"])
        self.assertIn("本次无Mod文件变更", manifest["notes"])

    def test_09_no_changes_duplicate_publish_rejected(self):
        """9. Duplicate publish with no file changes and no note changes is rejected"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-static.jar").write_bytes(b"STATIC_CONTENT")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        self._http_request("POST", "/api/admin/publish", data={"notes": "不变公告", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)

        # Try publishing again with identical notes and digest
        status, _, body = self._http_request(
            "POST", "/api/admin/publish",
            data={"notes": "不变公告", "scan_digest": digest},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 400)
        data = json.loads(body.decode())
        self.assertEqual(data.get("code"), "NO_CHANGES")

    def test_10_unreleased_file_cannot_be_downloaded(self):
        """10. Files newly added to directory but not published cannot be downloaded"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-published.jar").write_bytes(b"PUBLISHED_JAR")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        self._http_request("POST", "/api/admin/publish", data={"notes": "v1", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)

        # Put new unpublished file
        (test_mods_dir / "mod-secret-unreleased.jar").write_bytes(b"SECRET")

        # Try downloading by an imaginary or made-up id
        fake_id = hashlib.sha256(b"SECRET").hexdigest()[:16]
        status, _, body = self._http_request(
            "GET", f"/api/v1/files/{fake_id}",
            headers={"Authorization": "Bearer test_client_token_abc"}
        )
        self.assertEqual(status, 404)

    def test_11_file_integrity_check_on_download(self):
        """11. Published file modified on disk after publish fails download with integrity conflict"""
        cookie, csrf = self._admin_login()
        target = test_mods_dir / "mod-tampered.jar"
        target.write_bytes(b"ORIGINAL_VALID_CONTENT")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        status, _, body = self._http_request("POST", "/api/admin/publish", data={"notes": "v1", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)
        manifest = json.loads(body.decode())["manifest"]
        file_id = manifest["files"][0]["id"]

        # 1. Download valid file
        status, _, body = self._http_request(
            "GET", f"/api/v1/files/{file_id}",
            headers={"Authorization": "Bearer test_client_token_abc"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ORIGINAL_VALID_CONTENT")
        self.assertEqual(hashlib.sha256(body).hexdigest(), manifest["files"][0]["sha256"])

        # 2. Tamper the file on disk without publishing
        target.write_bytes(b"TAMPERED_CONTENT_WITHOUT_PUBLISH")

        # 3. Download must be rejected with 409 Conflict
        status, _, body = self._http_request(
            "GET", f"/api/v1/files/{file_id}",
            headers={"Authorization": "Bearer test_client_token_abc"}
        )
        self.assertEqual(status, 409)
        data = json.loads(body.decode())
        self.assertEqual(data.get("error"), "FILE_INTEGRITY_MISMATCH")

        # 4. Delete the file on disk without publishing
        target.unlink()
        status, _, body = self._http_request(
            "GET", f"/api/v1/files/{file_id}",
            headers={"Authorization": "Bearer test_client_token_abc"}
        )
        self.assertEqual(status, 410)

    def test_12_disable_and_enable_download(self):
        """12. When download is disabled, latest manifest and file downloads are refused (503)"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-service.jar").write_bytes(b"SERVICE_JAR")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        digest = json.loads(body.decode())["stats"]["scan_digest"]
        status, _, body = self._http_request("POST", "/api/admin/publish", data={"notes": "v1", "scan_digest": digest}, headers={"X-CSRF-Token": csrf}, cookie=cookie)
        manifest = json.loads(body.decode())["manifest"]
        file_id = manifest["files"][0]["id"]

        # Disable download
        status, _, body = self._http_request(
            "POST", "/api/admin/toggle-download",
            data={"enabled": False},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body.decode())["download_enabled"])

        # Latest query rejected with 503
        status, _, body = self._http_request(
            "GET", "/api/v1/latest",
            headers={"Authorization": "Bearer test_client_token_abc"}
        )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body.decode())["error"], "SERVICE_DISABLED")

        # File download rejected with 503
        status, _, body = self._http_request(
            "GET", f"/api/v1/files/{file_id}",
            headers={"Authorization": "Bearer test_client_token_abc"}
        )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body.decode())["error"], "SERVICE_DISABLED")

        # Re-enable download
        status, _, body = self._http_request(
            "POST", "/api/admin/toggle-download",
            data={"enabled": True},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode())["download_enabled"])

        # Download works again
        status, _, body = self._http_request(
            "GET", f"/api/v1/files/{file_id}",
            headers={"Authorization": "Bearer test_client_token_abc"}
        )
        self.assertEqual(status, 200)

    def test_13_empty_directory_confirmation(self):
        """13. Empty directory requires explicit confirmation to publish"""
        cookie, csrf = self._admin_login()
        # Scan empty directory
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertTrue(data["is_empty"])
        digest = data["stats"]["scan_digest"]

        # Publish without allow_empty
        status, _, body = self._http_request(
            "POST", "/api/admin/publish",
            data={"notes": "清空发布", "scan_digest": digest, "allow_empty": False},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body.decode())["code"], "EMPTY_DIRECTORY_CONFIRMATION_REQUIRED")

        # Publish with allow_empty = True
        status, _, body = self._http_request(
            "POST", "/api/admin/publish",
            data={"notes": "清空发布", "scan_digest": digest, "allow_empty": True},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        manifest = json.loads(body.decode())["manifest"]
        self.assertEqual(len(manifest["files"]), 0)

    def test_14_stale_scan_detection(self):
        """14. If files change between preview and publish, publish fails with SCAN_OUTDATED"""
        cookie, csrf = self._admin_login()
        (test_mods_dir / "mod-alpha.jar").write_bytes(b"ALPHA")
        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        stale_digest = json.loads(body.decode())["stats"]["scan_digest"]

        # Directory changes behind the scenes
        (test_mods_dir / "mod-beta.jar").write_bytes(b"BETA")

        # Attempt publish with stale digest
        status, _, body = self._http_request(
            "POST", "/api/admin/publish",
            data={"notes": "v1", "scan_digest": stale_digest},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 400)
        data = json.loads(body.decode())
        self.assertEqual(data.get("code"), "SCAN_OUTDATED")

    def test_15_ignore_non_jar_and_subdirectories(self):
        """15. Subdirectories, non-jar files, hidden files, and temp files are strictly ignored"""
        cookie, csrf = self._admin_login()
        # Top-level valid jar
        (test_mods_dir / "valid.jar").write_bytes(b"VALID")
        # Subdirectory with jar
        subdir = test_mods_dir / "subdir"
        subdir.mkdir()
        (subdir / "nested.jar").write_bytes(b"NESTED")
        # Non-jar files
        (test_mods_dir / "config.txt").write_bytes(b"CONFIG")
        (test_mods_dir / ".hidden.jar").write_bytes(b"HIDDEN")
        (test_mods_dir / "temp.jar.tmp").write_bytes(b"TEMP")
        (test_mods_dir / "temp.jar.part").write_bytes(b"PART")

        status, _, body = self._http_request("POST", "/api/admin/scan", headers={"X-CSRF-Token": csrf}, cookie=cookie)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        # Only valid.jar should be detected!
        self.assertEqual(data["stats"]["total_count"], 1)
        self.assertEqual(data["files"][0]["filename"], "valid.jar")

    def test_16_admin_templates_and_auth(self):
        """16. Template list requires admin login and returns available templates"""
        # Unauthenticated request
        status, _, _ = self._http_request("GET", "/api/admin/templates")
        self.assertEqual(status, 401)

        # Authenticated request
        cookie, csrf = self._admin_login()
        status, _, body = self._http_request("GET", "/api/admin/templates", cookie=cookie)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertTrue(data["success"])
        self.assertIsInstance(data["templates"], list)
        self.assertGreater(len(data["templates"]), 0)
        tpl = data["templates"][0]
        self.assertEqual(tpl["id"], "neoforge-26.1.2")
        self.assertEqual(tpl["status"], "verified")

    def test_17_generate_updater_jar_integrity_and_config(self):
        """17. Admin can generate updater JAR with embedded private configuration"""
        cookie, csrf = self._admin_login()
        endpoint = "http://192.168.1.100:25580"

        # 1. Unauthenticated generation is rejected
        status, _, _ = self._http_request(
            "POST", "/api/admin/generate-updater",
            data={"template_id": "neoforge-26.1.2", "client_endpoint": endpoint}
        )
        self.assertEqual(status, 401)

        # 2. Generation with invalid endpoint is rejected
        status, _, body = self._http_request(
            "POST", "/api/admin/generate-updater",
            data={"template_id": "neoforge-26.1.2", "client_endpoint": "not-a-url"},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 400)

        # 3. Successful generation
        status, headers, body = self._http_request(
            "POST", "/api/admin/generate-updater",
            data={"template_id": "neoforge-26.1.2", "client_endpoint": endpoint},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/java-archive")
        self.assertIn("modsync-neoforge-26.1.2-client.jar", headers.get("Content-Disposition", ""))

        # 4. Inspect generated JAR in memory: verify modsync-server.json has correct config injected
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(body), "r") as z:
            cfg_bytes = z.read("modsync-server.json")
            cfg = json.loads(cfg_bytes.decode("utf-8"))
            self.assertEqual(cfg["endpoint"], endpoint)
            self.assertEqual(cfg["packId"], "test-pack")
            self.assertEqual(cfg["token"], "test_client_token_abc")

    def test_18_generate_forge_updater_jar(self):
        """18. Admin can generate Forge 1.20.1 updater JAR with correct metadata and config"""
        cookie, csrf = self._admin_login()
        endpoint = "http://192.168.1.100:25580"

        status, headers, body = self._http_request(
            "POST", "/api/admin/generate-updater",
            data={"template_id": "forge-1.20.1", "client_endpoint": endpoint},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/java-archive")
        self.assertIn("modsync-forge-1.20.1-client.jar", headers.get("Content-Disposition", ""))

        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(body), "r") as z:
            cfg_bytes = z.read("modsync-server.json")
            cfg = json.loads(cfg_bytes.decode("utf-8"))
            self.assertEqual(cfg["endpoint"], endpoint)
            self.assertEqual(cfg["packId"], "test-pack")
            self.assertEqual(cfg["token"], "test_client_token_abc")

            mods_toml = z.read("META-INF/mods.toml").decode("utf-8")
            self.assertIn('modId="modsync"', mods_toml)
            self.assertIn('modId="forge"', mods_toml)
            self.assertIn('versionRange="[1.20.1, 1.20.2)"', mods_toml)

    def test_19_runtime_config_api(self):
        """19. Admin can get and update runtime configuration dynamically"""
        cookie, csrf = self._admin_login()

        # Get config
        status, _, body = self._http_request("GET", "/api/admin/config", cookie=cookie)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertTrue(data["success"])
        self.assertIn("pack_id", data["config"])

        # Update pack_id and endpoint
        status, _, body = self._http_request(
            "POST", "/api/admin/config",
            data={"pack_id": "new-test-pack", "client_endpoint": "http://192.168.1.50:25580"},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["config"]["pack_id"], "new-test-pack")
        self.assertEqual(data["config"]["client_endpoint"], "http://192.168.1.50:25580")

        # Reset back to test-pack
        self._http_request(
            "POST", "/api/admin/config",
            data={"pack_id": "test-pack", "client_endpoint": ""},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )

    def test_20_change_password_and_rotate_token(self):
        """20. Admin can change password and rotate client token"""
        cookie, csrf = self._admin_login()

        # 1. Rotate token
        status, _, body = self._http_request(
            "POST", "/api/admin/rotate-token",
            data={}, headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        new_token = data["client_token"]
        self.assertNotEqual(new_token, "test_client_token_abc")

        # 2. Verify new token works for latest API
        status, _, _ = self._http_request("GET", "/api/v1/latest", headers={"Authorization": f"Bearer {new_token}"})
        self.assertIn(status, [200, 404, 503]) # authenticated

        # 3. Change password
        status, _, body = self._http_request(
            "POST", "/api/admin/change-password",
            data={"old_password": "test_admin_pass_123", "new_password": "new_secret_password_456"},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)

        # 4. Old password fails
        status, _, _ = self._http_request("POST", "/api/admin/login", {"password": "test_admin_pass_123"})
        self.assertEqual(status, 401)

        # 5. New password succeeds
        status, _, body = self._http_request("POST", "/api/admin/login", {"password": "new_secret_password_456"})
        self.assertEqual(status, 200)

        # Reset back for subsequent test cleanups
        publisher.update_state({"admin_password_hash": None, "client_token": "test_client_token_abc"})

    def test_21_directory_selection_and_path_traversal_boundaries(self):
        """21. Test directory selection within allowed root and rejection of escape attempts"""
        cookie, csrf = self._admin_login()

        # Set up a test DIST_ROOT
        test_dist = Path(test_tmp) / "dist_root"
        sub_a = test_dist / "pack_a_mods"
        sub_b = test_dist / "pack_b_mods"
        sub_a.mkdir(parents=True, exist_ok=True)
        sub_b.mkdir(parents=True, exist_ok=True)

        config.DIST_ROOT = test_dist
        config.HOST_DIST_ROOT = "/opt/minecraft/dist"

        # 1. Available subdirs listed
        status, _, body = self._http_request("GET", "/api/admin/config", cookie=cookie)
        self.assertEqual(status, 200)
        cfg = json.loads(body.decode())["config"]
        self.assertIn("pack_a_mods", cfg["available_subdirs"])
        self.assertIn("pack_b_mods", cfg["available_subdirs"])

        # 2. Select valid subdir
        status, _, body = self._http_request(
            "POST", "/api/admin/config",
            data={"mods_subdir": "pack_a_mods"},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 200)
        cfg = json.loads(body.decode())["config"]
        self.assertEqual(cfg["mods_subdir"], "pack_a_mods")
        self.assertEqual(cfg["effective_host_path"], "/opt/minecraft/dist/pack_a_mods")

        # 3. Path traversal escape attempt rejected
        status, _, body = self._http_request(
            "POST", "/api/admin/config",
            data={"mods_subdir": "../../etc"},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 400)
        data = json.loads(body.decode())
        self.assertEqual(data.get("error"), "INVALID_SUBDIR")

        # 4. Non-existent subdir rejected
        status, _, body = self._http_request(
            "POST", "/api/admin/config",
            data={"mods_subdir": "non_existent_subdir_123"},
            headers={"X-CSRF-Token": csrf}, cookie=cookie
        )
        self.assertEqual(status, 400)
        data = json.loads(body.decode())
        self.assertEqual(data.get("error"), "SUBDIR_NOT_FOUND")

        # Reset DIST_ROOT back
        config.DIST_ROOT = None
        config.HOST_DIST_ROOT = ""
        publisher.update_state({"mods_subdir": "client_mods"})

if __name__ == "__main__":
    unittest.main()

