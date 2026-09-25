import os
from pathlib import Path

class Config:
    def __init__(self):
        self.PORT = int(os.environ.get("PORT", "25580"))
        self.PACK_ID = os.environ.get("PACK_ID", "default-pack")
        self.APP_NAME = os.environ.get("APP_NAME", "ModSync 更新发布器")
        self.HOST_MODS_DIR = os.environ.get("HOST_MODS_DIR", "./client_mods")
        self.DEFAULT_CLIENT_ENDPOINT = os.environ.get("CLIENT_ENDPOINT", "")

        # In-container paths
        self.MODS_DIR = Path(os.environ.get("MODS_DIR", "/data/mods")).resolve()
        self.STATE_DIR = Path(os.environ.get("STATE_DIR", "/data/state")).resolve()
        self.TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", Path(__file__).parent / "mod_templates")).resolve()

        # Credentials
        self.ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
        self.CLIENT_TOKEN = os.environ.get("CLIENT_TOKEN", "")

        # Performance and security
        self.SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "24"))
        self.RATE_LIMIT_RPS = int(os.environ.get("RATE_LIMIT_RPS", "30"))
        self.MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "10"))

config = Config()
