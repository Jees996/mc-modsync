import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

class ScannerError(Exception):
    pass

class DirectoryUnavailableError(ScannerError):
    pass

class FileChangingError(ScannerError):
    pass

def compute_file_sha256(filepath: Path) -> Tuple[str, int]:
    """
    Computes SHA-256 and file size.
    Checks file stability (mtime and size) before and after reading to detect ongoing copies.
    """
    try:
        stat_before = filepath.stat()
    except OSError as e:
        raise ScannerError(f"无法读取文件状态: {filepath.name} ({e})")

    sha256 = hashlib.sha256()
    bytes_read = 0
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
                bytes_read += len(chunk)
    except OSError as e:
        raise ScannerError(f"读取文件内容失败: {filepath.name} ({e})")

    try:
        stat_after = filepath.stat()
    except OSError as e:
        raise ScannerError(f"无法验证文件状态: {filepath.name} ({e})")

    if stat_before.st_mtime != stat_after.st_mtime or stat_before.st_size != stat_after.st_size or bytes_read != stat_after.st_size:
        raise FileChangingError(f"文件正在写入或变动中: {filepath.name}，请等待文件复制完成后重试")

    return sha256.hexdigest(), bytes_read

def scan_mods_directory(mods_dir: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Scans the mods directory for top-level .jar files only.
    Filters out hidden files, temp files, symlinks, and directories.
    Returns (sorted_file_entries, stats_dict).
    """
    if not mods_dir.exists():
        raise DirectoryUnavailableError(f"发布目录不存在或未挂载: {mods_dir}")
    if not mods_dir.is_dir():
        raise DirectoryUnavailableError(f"发布目录路径不是目录: {mods_dir}")

    files: List[Dict[str, Any]] = []

    try:
        entries = list(mods_dir.iterdir())
    except OSError as e:
        raise DirectoryUnavailableError(f"扫描发布目录失败: {e}")

    for entry in entries:
        try:
            # Skip symlinks, directories, non-regular files
            if entry.is_symlink():
                continue
            if not entry.is_file():
                continue

            name = entry.name
            # Skip hidden files and temp files
            if name.startswith(".") or name.startswith("~"):
                continue
            lower_name = name.lower()
            if any(lower_name.endswith(ext) for ext in [".tmp", ".temp", ".part", ".crdownload", ".bak", ".swp"]):
                continue

            # Only accept .jar
            if not lower_name.endswith(".jar"):
                continue

            # Skip updater itself (never distribute the updater in the mods manifest)
            if lower_name.startswith("modsync") or lower_name.startswith("lee-updater"):
                continue

            file_hash, file_size = compute_file_sha256(entry)

            files.append({
                "filename": name,
                "path_rel": f"mods/{name}",
                "size": file_size,
                "sha256": file_hash
            })
        except OSError as e:
            raise ScannerError(f"检查文件出错: {entry.name} ({e})")

    # Sort deterministically by filename
    files.sort(key=lambda x: x["filename"].lower())

    # Build scan digest of all files and their hashes
    hasher = hashlib.sha256()
    for f in files:
        hasher.update(f"{f['filename']}:{f['sha256']}\n".encode("utf-8"))
    scan_digest = hasher.hexdigest()

    stats = {
        "total_count": len(files),
        "total_size": sum(f["size"] for f in files),
        "scan_digest": scan_digest
    }

    return files, stats

def diff_scans(current_files: List[Dict[str, Any]], previous_files: Optional[List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    """
    Compares current files with previous release files.
    Returns:
    {
        "added": [...],
        "modified": [...],
        "deleted": [...],
        "unchanged": [...]
    }
    """
    curr_map = {f["filename"]: f["sha256"] for f in current_files}

    if previous_files is None:
        # First release: all files are added
        return {
            "added": sorted(list(curr_map.keys()), key=str.lower),
            "modified": [],
            "deleted": [],
            "unchanged": []
        }

    prev_map = {f["filename"]: f["sha256"] for f in previous_files}

    added = []
    modified = []
    deleted = []
    unchanged = []

    for fn, sha in curr_map.items():
        if fn not in prev_map:
            added.append(fn)
        else:
            if sha != prev_map[fn]:
                modified.append(fn)
            else:
                unchanged.append(fn)

    for fn in prev_map:
        if fn not in curr_map:
            deleted.append(fn)

    return {
        "added": sorted(added, key=str.lower),
        "modified": sorted(modified, key=str.lower),
        "deleted": sorted(deleted, key=str.lower),
        "unchanged": sorted(unchanged, key=str.lower)
    }

def format_change_summary(changes: Dict[str, List[str]]) -> str:
    """
    Formats the file changes into Chinese text:
    Mod文件变化：
    新增（2）：
    + example-a.jar
    + example-b.jar

    更新（1）：
    ~ example-c.jar

    删除（1）：
    - example-old.jar
    """
    added = changes.get("added", [])
    modified = changes.get("modified", [])
    deleted = changes.get("deleted", [])

    if not added and not modified and not deleted:
        return "Mod文件变化：\n本次无Mod文件变更"

    parts = ["Mod文件变化："]
    if added:
        lines = [f"+ {fn}" for fn in added]
        parts.append(f"新增（{len(added)}）：\n" + "\n".join(lines))
    if modified:
        lines = [f"~ {fn}" for fn in modified]
        parts.append(f"更新（{len(modified)}）：\n" + "\n".join(lines))
    if deleted:
        lines = [f"- {fn}" for fn in deleted]
        parts.append(f"删除（{len(deleted)}）：\n" + "\n".join(lines))

    return "\n\n".join(parts)

def compose_notes(user_notes: str, changes: Dict[str, List[str]]) -> str:
    """
    Combines user notes with the generated file change summary.
    Removes any previously existing auto-generated 'Mod文件变化：' block to prevent duplicate accumulation.
    """
    cleaned_notes = user_notes.strip()
    # Strip any existing Mod文件变化 section
    marker = "Mod文件变化："
    if marker in cleaned_notes:
        idx = cleaned_notes.find(marker)
        cleaned_notes = cleaned_notes[:idx].rstrip()

    change_text = format_change_summary(changes)
    if cleaned_notes:
        return f"{cleaned_notes}\n\n{change_text}"
    else:
        return change_text
