import hashlib
import io
import json
import logging
import urllib.parse
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .config import config

logger = logging.getLogger("mc-publisher")

class GeneratorError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

def get_templates_file() -> Path:
    return config.TEMPLATES_DIR / "templates.json"

def get_available_templates() -> List[Dict[str, Any]]:
    tf = get_templates_file()
    if not tf.exists():
        return []
    try:
        with open(tf, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("读取模板清单失败: %s", e)
        return []

def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    for t in get_available_templates():
        if t.get("id") == template_id:
            return t
    return None

def generate_configured_jar(template_id: str, endpoint: str, pack_id: Optional[str] = None, token: Optional[str] = None) -> Tuple[bytes, str]:
    """
    根据选定的模板 ID 与客户端访问地址，动态生成内置私有配置的客户端更新器 JAR。
    全程内存流式处理，不修改原始模板，不运行任何外部构建命令。
    """
    template = get_template(template_id)
    if not template:
        raise GeneratorError("TEMPLATE_NOT_FOUND", f"未找到指定的更新器模板: {template_id}")

    if template.get("status") not in ("verified", "experimental"):
        raise GeneratorError("TEMPLATE_UNSUPPORTED", "该模板当前不受支持或未就绪")

    filename = template.get("filename", "")
    template_path = config.TEMPLATES_DIR / filename
    if not template_path.is_file():
        raise GeneratorError("TEMPLATE_FILE_MISSING", f"模板文件缺失: {filename}")

    # 校验模板 SHA-256 哈希完整性
    expected_sha = template.get("sha256", "")
    with open(template_path, "rb") as f:
        actual_sha = hashlib.sha256(f.read()).hexdigest()
    if expected_sha and actual_sha.lower() != expected_sha.lower():
        raise GeneratorError("TEMPLATE_INTEGRITY_FAILED", "模板文件 SHA-256 校验失败，文件可能已损坏")

    # 校验并规范化客户端 endpoint
    endpoint_clean = endpoint.strip().rstrip("/")
    if not endpoint_clean:
        raise GeneratorError("INVALID_ENDPOINT", "客户端访问地址不能为空")
    try:
        parsed = urllib.parse.urlsplit(endpoint_clean)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError()
    except Exception:
        raise GeneratorError("INVALID_ENDPOINT", "客户端访问地址格式无效，必须以 http:// 或 https:// 开头并包含有效主机名或IP")

    effective_pack_id = pack_id or config.PACK_ID
    effective_token = token or config.CLIENT_TOKEN
    if not effective_token:
        raise GeneratorError("TOKEN_NOT_CONFIGURED", "服务端尚未配置 CLIENT_TOKEN，无法生成更新器")

    # 组装待嵌入的客户端私有配置
    server_config_data = {
        "endpoint": endpoint_clean,
        "packId": effective_pack_id,
        "token": effective_token
    }
    config_json_bytes = json.dumps(server_config_data, ensure_ascii=False, indent=2).encode("utf-8")

    # 在内存中复制 JAR 并替换配置文件
    out_buffer = io.BytesIO()
    with zipfile.ZipFile(template_path, "r") as zin:
        with zipfile.ZipFile(out_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                # 遇到配置资源文件时替换为真实内容
                if item.filename in ("modsync-server.json", "lee-updater-server.json"):
                    zout.writestr(item.filename, config_json_bytes)
                else:
                    zout.writestr(item, zin.read(item.filename))

    out_bytes = out_buffer.getvalue()
    loader = template.get("loader", "mod").lower()
    mc_ver = template.get("id", "").replace(f"{loader}-", "")
    out_filename = f"modsync-{loader}-{mc_ver}-client.jar"
    return out_bytes, out_filename
