# ModSync 接口规范文档 (v1)

本规范用于 Minecraft 客户端模组同步。客户端 Mod 可通过标准化接口完成公告拉取、增量差异比对与文件下载覆盖；服务端提供管理与更新器生成接口。

---

## 基础信息

- **基础 URL 示例**: `http://<server-host>:<port>`（如 `http://192.168.1.100:25580`）
- **协议**: HTTP/1.1
- **数据编码**: JSON (`Content-Type: application/json; charset=utf-8`)
- **客户端鉴权**: `Authorization: Bearer <CLIENT_TOKEN>`（除 `/healthz` 外所有客户端接口均需提供）

---

## 客户端接口定义

### 1. 服务健康检查

- **路径**: `GET /healthz`
- **鉴权**: 无需鉴权
- **用途**: 检测发布器服务进程与网络是否存活
- **响应示例 (200 OK)**:
  ```json
  {
    "status": "ok"
  }
  ```

---

### 2. 获取最新发布清单

- **路径**: `GET /api/v1/latest`
- **鉴权**: `Authorization: Bearer <CLIENT_TOKEN>`
- **用途**: 获取当前最后一次成功发布的整合包版本、更新说明和完整目标文件列表。
- **说明**:
  - `files` 是客户端判断最终目标状态的依据。客户端应比对本地 `.minecraft/mods` 目录：
    - 本地缺少的文件：根据 `download_path` 请求下载。
    - 本地已存在但 SHA-256 不一致的文件：根据设置覆盖或跳过。
    - 本地存在但不在 `files` 清单中的旧受管文件：普通更新模式下清理。
    - 未知自装模组：普通更新模式下安全保留。
  - `changes` 仅用于向玩家在游戏界面展示更新公告与变动概览，不能作为跨多版本更新的唯一比对依据。
- **响应示例 (200 OK)**:
  ```json
  {
    "schema_version": 1,
    "pack_id": "example-pack",
    "release_id": "rel_20260925_030000_a1b2",
    "published_at": "2026-09-25T03:00:00+08:00",
    "notes": "本次更新：\n修复地图问题，调整部分配方。\n\nMod文件变化：\n新增（2）：\n+ example-a.jar\n+ example-b.jar\n\n更新（1）：\n~ example-c.jar\n\n删除（1）：\n- example-old.jar",
    "changes": {
      "added": [
        "example-a.jar",
        "example-b.jar"
      ],
      "modified": [
        "example-c.jar"
      ],
      "deleted": [
        "example-old.jar"
      ]
    },
    "files": [
      {
        "id": "c65bf643206113d9",
        "filename": "example-a.jar",
        "path": "mods/example-a.jar",
        "size": 2512274,
        "sha256": "c65bf643206113d9abcdef...",
        "download_path": "/api/v1/files/c65bf643206113d9"
      }
    ]
  }
  ```
- **错误状态码**:
  - **401 Unauthorized**: 缺少 `Authorization` 请求头。
  - **403 Forbidden**: 客户端 Token 错误。
  - **404 Not Found**: 尚未发布任何版本 (`{"error": "NOT_PUBLISHED", "message": "尚未发布任何整合包版本"}`)。
  - **503 Service Unavailable**: 下载服务已在后台被管理员停用维护 (`{"error": "SERVICE_DISABLED", "message": "客户端更新与下载服务当前已停用维护中"}`)。

---

### 3. 下载指定 Mod 文件

- **路径**: `GET /api/v1/files/{file_id}`
- **鉴权**: `Authorization: Bearer <CLIENT_TOKEN>`
- **用途**: 下载已发布清单中的单个 `.jar` 文件。
- **响应头**:
  - `Content-Type: application/java-archive`
  - `Content-Length: <文件字节大小>`
  - `Content-Disposition: attachment; filename="<文件名>"; filename*=UTF-8''<文件名>`
- **重要约束与一致性保障**:
  - 服务端采用不透明 `file_id`，杜绝任意路径穿越。
  - **下载前强一致性校验**：服务端在传输前会校验源文件当前哈希是否仍与发布清单一致。若管理员在发布后替换了文件而未重新发布，服务端将返回 `409 Conflict` 明确拒绝，防止客户端下载到未核准的不一致文件。
  - **客户端双重校验要求**：客户端在下载完成后，必须自行计算本地文件的 SHA-256 并与清单比对，若不一致应提示重试或中止更新，不得使用破损文件启动游戏。
- **错误状态码**:
  - **401 Unauthorized**: 缺少 Token。
  - **403 Forbidden**: Token 错误或非法路径。
  - **404 Not Found**: 请求的 `file_id` 不存在。
  - **409 Conflict**: `{"error": "FILE_INTEGRITY_MISMATCH", "message": "源文件在发布后已被修改，拒绝提供不一致的下载"}`。
  - **410 Gone**: `{"error": "FILE_DELETED_ON_DISK", "message": "源文件已被移除但尚未重新发布"}`。
  - **429 Too Many Requests**: 并发下载数或请求频率超限。
  - **503 Service Unavailable**: 下载服务已停用。

---

## 管理端更新器生成接口

### 4. 获取更新器模板列表

- **路径**: `GET /api/admin/templates`
- **鉴权**: 管理员会话 Cookie (`mc_admin_session`)
- **响应示例 (200 OK)**:
  ```json
  {
    "success": true,
    "templates": [
      {
        "id": "neoforge-26.1.2",
        "loader": "NeoForge",
        "mc_range": "[26.1.2, 26.2)",
        "loader_range": "[26.1.2.100, )",
        "status": "verified"
      }
    ],
    "default_endpoint": "http://192.168.1.100:25580",
    "pack_id": "example-pack"
  }
  ```

---

### 5. 一键生成并下载客户端更新器

- **路径**: `POST /api/admin/generate-updater`
- **鉴权**: 管理员会话 Cookie + `X-CSRF-Token` 请求头
- **请求体 (JSON)**:
  ```json
  {
    "template_id": "neoforge-26.1.2",
    "client_endpoint": "http://192.168.1.100:25580"
  }
  ```
- **响应**: 直接返回二进制流 (`Content-Type: application/java-archive`) 并附带 `Content-Disposition` 触发浏览器下载。
