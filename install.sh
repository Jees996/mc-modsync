#!/usr/bin/env bash
# ==============================================================================
# ModSync 一键部署与管理脚本 (Linux / Docker)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${SCRIPT_DIR}/deploy"
AUTO_YES=false

for arg in "$@"; do
    case "$arg" in
        -y|--yes)
            AUTO_YES=true
            ;;
    esac
done

echo "=================================================="
echo "          ModSync 服务端一键部署向导              "
echo "=================================================="

# 1. 检查 Docker 环境
if ! command -v docker &> /dev/null; then
    echo "[!] 未检测到 Docker 运行环境。"
    if [ -f /etc/debian_version ] || grep -qi "ubuntu" /etc/os-release 2>/dev/null; then
        INSTALL_DOCKER=false
        if [ "$AUTO_YES" = true ]; then
            INSTALL_DOCKER=true
        else
            read -r -p "检测到系统支持 apt，是否自动通过 apt 安装 docker.io 与 docker-compose-v2？[y/N]: " choice
            case "$choice" in
                [yY][eE][sS]|[yY]) INSTALL_DOCKER=true ;;
                *) INSTALL_DOCKER=false ;;
            esac
        fi

        if [ "$INSTALL_DOCKER" = true ]; then
            echo "[*] 正在安装 docker.io 与 docker-compose-v2..."
            if command -v sudo &> /dev/null; then
                sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
            else
                apt-get update && apt-get install -y docker.io docker-compose-v2
            fi
            echo "[+] Docker 安装完成。"
        else
            echo "[-] 用户已取消安装。请先手动安装 Docker 和 Docker Compose 后重试。"
            exit 1
        fi
    else
        echo "[-] 请先根据您的 Linux 发行版安装 Docker 与 Docker Compose 插件后再运行本脚本。"
        exit 1
    fi
fi

# 检查 Docker 权限
DOCKER_CMD="docker"
if ! docker ps &> /dev/null; then
    if command -v sudo &> /dev/null && sudo -n docker ps &> /dev/null; then
        DOCKER_CMD="sudo docker"
    else
        echo "[*] 当前用户 $(whoami) 未在 docker 用户组或无权限访问 /var/run/docker.sock。"
        if command -v sudo &> /dev/null; then
            echo "[*] 尝试将当前用户加入 docker 组..."
            sudo usermod -aG docker "$USER" 2>/dev/null || true
            echo "[!] 已将 $USER 加入 docker 组。若本次执行提示权限不足，请执行 'newgrp docker' 或使用 sudo。"
            DOCKER_CMD="sudo docker"
        fi
    fi
fi

# 检查 Docker Compose
if ! $DOCKER_CMD compose version &> /dev/null; then
    if command -v docker-compose &> /dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        echo "[-] 未检测到 docker compose 插件。请安装 docker-compose-v2 后重试。"
        exit 1
    fi
else
    COMPOSE_CMD="$DOCKER_CMD compose"
fi

# 2. 准备目录结构
DIST_ROOT="${SCRIPT_DIR}/dist"
CLIENT_MODS_DIR="${DIST_ROOT}/client_mods"
STATE_DIR="${DEPLOY_DIR}/state"

mkdir -p "$CLIENT_MODS_DIR"
mkdir -p "$STATE_DIR"
chmod 755 "$DIST_ROOT" "$CLIENT_MODS_DIR" "$STATE_DIR" 2>/dev/null || true

# 3. 初始化/保持 .env 私有配置
ENV_FILE="${DEPLOY_DIR}/.env"
IS_NEW_INSTALL=false

# 自动获取局域网 IPv4
LAN_IP="127.0.0.1"
if command -v ip &> /dev/null; then
    LAN_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7}' | tr -d '\n' || true)
fi
if [ -z "$LAN_IP" ] && command -v hostname &> /dev/null; then
    LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
fi
[ -z "$LAN_IP" ] && LAN_IP="127.0.0.1"

CURRENT_UID=$(id -u)
CURRENT_GID=$(id -g)

if [ ! -f "$ENV_FILE" ]; then
    IS_NEW_INSTALL=true
    echo "[*] 正在为首次部署生成独立的随机安全凭据..."

    # 生成随机密码与只读 Token
    GEN_ADMIN_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))" 2>/dev/null || head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)
    GEN_CLIENT_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))" 2>/dev/null || head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)

    cat <<EOF > "$ENV_FILE"
PORT=25580
PACK_ID=modsync-pack
APP_NAME=ModSync 更新发布器
HOST_DIST_ROOT=${DIST_ROOT}
MODS_SUBDIR=client_mods
CLIENT_ENDPOINT=http://${LAN_IP}:25580
ADMIN_PASSWORD=${GEN_ADMIN_PASS}
CLIENT_TOKEN=${GEN_CLIENT_TOKEN}
MODSYNC_UID=${CURRENT_UID}
MODSYNC_GID=${CURRENT_GID}
RATE_LIMIT_RPS=30
MAX_CONCURRENT_DOWNLOADS=10
EOF
    chmod 600 "$ENV_FILE"
    echo "[+] 私有配置文件已创建: ${ENV_FILE}"
else
    echo "[*] 检测到已存在私有配置 ${ENV_FILE}，已完整保留现有密码、Token与参数。"
    # 补充 UID/GID 与 HOST_DIST_ROOT 避免旧配置缺失
    grep -q "^MODSYNC_UID=" "$ENV_FILE" || echo "MODSYNC_UID=${CURRENT_UID}" >> "$ENV_FILE"
    grep -q "^MODSYNC_GID=" "$ENV_FILE" || echo "MODSYNC_GID=${CURRENT_GID}" >> "$ENV_FILE"
    grep -q "^HOST_DIST_ROOT=" "$ENV_FILE" || echo "HOST_DIST_ROOT=${DIST_ROOT}" >> "$ENV_FILE"
fi

# 4. 检查端口占用
PORT=$(grep "^PORT=" "$ENV_FILE" | cut -d= -f2 | tr -d '\r\n')
[ -z "$PORT" ] && PORT=25580

if command -v ss &> /dev/null; then
    if ss -tulpn 2>/dev/null | grep -q ":${PORT} "; then
        # 检查是否正是我们的旧容器在占用
        if ! $DOCKER_CMD ps --format '{{.Names}}' 2>/dev/null | grep -q "modsync-server"; then
            echo "[!] 警告：端口 ${PORT} 当前已被其他系统进程占用！请修改 ${ENV_FILE} 中的 PORT 后重新执行。"
            exit 1
        fi
    fi
fi

# 5. 构建并启动容器
echo "[*] 正在构建 ModSync 服务端容器镜像..."
(cd "$DEPLOY_DIR" && $COMPOSE_CMD build)

echo "[*] 正在启动 ModSync 服务容器..."
(cd "$DEPLOY_DIR" && $COMPOSE_CMD up -d)

# 6. 健康检查验证
echo "[*] 正在等待服务就绪..."
HEALTHY=false
for i in {1..15}; do
    if curl -s "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"ok"'; then
        HEALTHY=true
        break
    fi
    sleep 2
done

if [ "$HEALTHY" = true ]; then
    echo "=================================================="
    echo "           🎉 ModSync 服务端已成功上线！           "
    echo "=================================================="
    echo "后台访问地址: http://${LAN_IP}:${PORT}"
    echo "宿主机分发目录: ${CLIENT_MODS_DIR}"
    echo "状态持久化路径: ${STATE_DIR}"
    echo "私有配置文件:   ${ENV_FILE}"

    if [ "$IS_NEW_INSTALL" = true ]; then
        echo ""
        echo "--------------------------------------------------"
        echo "【首次登录管理员初始密码】: ${GEN_ADMIN_PASS}"
        echo "【客户端只读下载 Token 】: ${GEN_CLIENT_TOKEN}"
        echo "--------------------------------------------------"
        echo "提示：请妥善保存初始密码。登录后台后可在网页随时修改密码与轮换 Token。"
    else
        echo ""
        echo "提示：已保留原有管理员密码与客户端 Token，密码见 ${ENV_FILE}"
    fi

    echo ""
    echo "日常运维管理命令："
    echo "  查看日志: $COMPOSE_CMD -f deploy/docker-compose.yml logs -f"
    echo "  重启服务: $COMPOSE_CMD -f deploy/docker-compose.yml restart"
    echo "  停止服务: $COMPOSE_CMD -f deploy/docker-compose.yml down"
    echo "=================================================="
else
    echo "[-] 服务启动后未能通过健康检查，请检查容器日志："
    (cd "$DEPLOY_DIR" && $COMPOSE_CMD logs --tail 20)
    exit 1
fi
