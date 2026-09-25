import datetime
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any

from .config import config
from .scanner import (
    scan_mods_directory,
    diff_scans,
    compose_notes,
    DirectoryUnavailableError,
    FileChangingError,
    ScannerError
)

class PublisherError(Exception):
    def __init__(self, code: str, message: str, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

class Publisher:
    def __init__(self, mods_dir: Path, state_dir: Path):
        self.mods_dir = mods_dir
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.manifest_file = self.state_dir / "manifest.json"
        self.state_file = self.state_dir / "state.json"

        self._lock = threading.RLock()
        self._init_state_if_needed()

    def _init_state_if_needed(self):
        with self._lock:
            if not self.state_file.exists():
                initial_state = {
                    "download_enabled": True,
                    "last_user_notes": "",
                    "sessions": {}
                }
                self._save_json_atomic(self.state_file, initial_state)

    def _save_json_atomic(self, target_path: Path, data: Any):
        temp_path = target_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)

    def get_manifest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self.manifest_file.exists():
                return None
            try:
                with open(self.manifest_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                return None

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            if not self.state_file.exists():
                return {"download_enabled": True, "last_user_notes": "", "sessions": {}}
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"download_enabled": True, "last_user_notes": "", "sessions": {}}

    def update_state(self, updates: Dict[str, Any]):
        with self._lock:
            state = self.get_state()
            state.update(updates)
            self._save_json_atomic(self.state_file, state)

    def set_download_enabled(self, enabled: bool) -> bool:
        self.update_state({"download_enabled": bool(enabled)})
        return bool(enabled)

    def is_download_enabled(self) -> bool:
        return bool(self.get_state().get("download_enabled", True))

    def preview_changes(self) -> Dict[str, Any]:
        """
        Scans current directory and returns diff against latest manifest.
        """
        files, stats = scan_mods_directory(self.mods_dir)
        manifest = self.get_manifest()
        prev_files = manifest.get("files", []) if manifest else None

        changes = diff_scans(files, prev_files)

        return {
            "stats": stats,
            "changes": changes,
            "is_empty": len(files) == 0,
            "has_changes": (len(changes["added"]) > 0 or len(changes["modified"]) > 0 or len(changes["deleted"]) > 0),
            "files": files,
            "previous_release_id": manifest.get("release_id") if manifest else None
        }

    def publish(self, user_notes: str, expected_scan_digest: str, allow_empty: bool = False) -> Dict[str, Any]:
        """
        Executes an atomic publish.
        Validates scan digest, checks empty directory rule, detects duplicate publish, writes new manifest.
        """
        if not self._lock.acquire(blocking=False):
            raise PublisherError("CONCURRENT_PUBLISH", "已有发布操作正在进行中，请勿重复点击")

        try:
            # 1. Re-scan the directory
            files, stats = scan_mods_directory(self.mods_dir)

            # 2. Check if digest matches what the user previewed
            if stats["scan_digest"] != expected_scan_digest:
                manifest = self.get_manifest()
                prev_files = manifest.get("files", []) if manifest else None
                new_changes = diff_scans(files, prev_files)
                raise PublisherError(
                    "SCAN_OUTDATED",
                    "发布目录在此期间发生了变动，已自动刷新文件预览，请重新确认后再发布",
                    data={
                        "stats": stats,
                        "changes": new_changes,
                        "is_empty": len(files) == 0
                    }
                )

            # 3. Check empty directory confirmation
            if len(files) == 0 and not allow_empty:
                raise PublisherError(
                    "EMPTY_DIRECTORY_CONFIRMATION_REQUIRED",
                    "发布目录中没有检测到任何 .jar 模组文件。若确需清空发布，请勾选确认框后再发布"
                )

            # 4. Compare with last manifest
            manifest = self.get_manifest()
            prev_files = manifest.get("files", []) if manifest else None
            changes = diff_scans(files, prev_files)

            has_file_changes = bool(changes["added"] or changes["modified"] or changes["deleted"])

            # Clean user notes
            user_notes_clean = user_notes.strip()
            marker = "Mod文件变化："
            if marker in user_notes_clean:
                idx = user_notes_clean.find(marker)
                user_notes_clean = user_notes_clean[:idx].rstrip()

            state = self.get_state()
            prev_notes = state.get("last_user_notes", "")

            notes_changed = (user_notes_clean != prev_notes)

            # If this is not first publish, and no file changes, and no notes change -> error
            if manifest is not None and not has_file_changes and not notes_changed:
                raise PublisherError("NO_CHANGES", "文件与更新说明均未发生变化，无需重复发布")

            # 5. Generate release metadata
            now = datetime.datetime.now().astimezone()
            now_iso = now.isoformat()
            timestamp_str = now.strftime("%Y%m%d_%H%M%S")
            random_suffix = secrets.token_hex(2)
            release_id = f"rel_{timestamp_str}_{random_suffix}"

            # 6. Compose final notes
            full_notes = compose_notes(user_notes_clean, changes)

            # 7. Generate manifest files list
            manifest_files = []
            for f in files:
                file_id = f["sha256"][:16]
                manifest_files.append({
                    "id": file_id,
                    "filename": f["filename"],
                    "path": f["path_rel"],
                    "size": f["size"],
                    "sha256": f["sha256"],
                    "download_path": f"/api/v1/files/{file_id}"
                })

            new_manifest = {
                "schema_version": 1,
                "pack_id": config.PACK_ID,
                "release_id": release_id,
                "published_at": now_iso,
                "notes": full_notes,
                "changes": {
                    "added": changes["added"],
                    "modified": changes["modified"],
                    "deleted": changes["deleted"]
                },
                "files": manifest_files
            }

            # 8. Atomic save manifest
            self._save_json_atomic(self.manifest_file, new_manifest)

            # 9. Update state
            state["last_user_notes"] = user_notes_clean
            state["last_release_id"] = release_id
            self._save_json_atomic(self.state_file, state)

            return new_manifest

        finally:
            self._lock.release()
