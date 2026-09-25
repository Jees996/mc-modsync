import hmac
import secrets
import time
from typing import Dict, Optional, Tuple, Any

from .config import config

class SessionStore:
    def __init__(self, ttl_seconds: int = 86400):
        self.ttl_seconds = ttl_seconds
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def create_session(self) -> Tuple[str, str]:
        token = secrets.token_hex(32)
        csrf_token = secrets.token_hex(16)
        now = time.time()
        self.sessions[token] = {
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
            "csrf_token": csrf_token
        }
        self.cleanup_expired()
        return token, csrf_token

    def get_session(self, token: str) -> Optional[Dict[str, Any]]:
        if not token or token not in self.sessions:
            return None
        sess = self.sessions[token]
        if time.time() > sess["expires_at"]:
            del self.sessions[token]
            return None
        return sess

    def delete_session(self, token: str):
        if token in self.sessions:
            del self.sessions[token]

    def cleanup_expired(self):
        now = time.time()
        expired = [t for t, s in self.sessions.items() if s["expires_at"] < now]
        for t in expired:
            del self.sessions[t]

session_store = SessionStore(ttl_seconds=config.SESSION_TTL_HOURS * 3600)

def verify_admin_password(password: str) -> bool:
    if not config.ADMIN_PASSWORD:
        return False
    return hmac.compare_digest(password.encode("utf-8"), config.ADMIN_PASSWORD.encode("utf-8"))

def verify_client_token(auth_header: Optional[str]) -> Tuple[bool, str, str]:
    """
    Checks Authorization: Bearer <CLIENT_TOKEN>
    Returns (is_valid, error_code, error_message)
    """
    if not auth_header:
        return False, "UNAUTHORIZED", "缺少认证凭据，请提供 Authorization: Bearer <token>"

    parts = auth_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False, "INVALID_TOKEN_FORMAT", "凭据格式错误，应为: Authorization: Bearer <token>"

    token = parts[1]
    if not config.CLIENT_TOKEN or not hmac.compare_digest(token.encode("utf-8"), config.CLIENT_TOKEN.encode("utf-8")):
        return False, "FORBIDDEN", "无效的客户端下载凭据"

    return True, "", ""
