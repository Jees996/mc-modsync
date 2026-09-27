# ModSync 客户端模组源码构建指南

本文档介绍如何从源码构建 ModSync 的客户端更新器通用模板。

---

## 环境准备

| 目标版本 | 目标加载器 | 推荐 JDK 版本 | 构建工具 |
| :--- | :--- | :--- | :--- |
| **NeoForge 26.1.2** | NeoForge 26.1.2+ | **JDK 25** | Gradle 9.2+ (内置 Wrapper) |
| **Forge 1.20.1** | Forge 47.1.0+ | **JDK 17** | Python 3.10+ & JDK 17 `javac` |

---

## 1. 构建 NeoForge 26.1.2 客户端更新器模板

NeoForge 26.1.2 采用官方推荐的 `net.neoforged.moddev` 插件管理构建。

### 构建步骤：

```bash
cd client
# 确保 JAVA_HOME 指向 JDK 25
export JAVA_HOME="/path/to/jdk-25"
export PATH="$JAVA_HOME/bin:$PATH"

# 编译生成无私有配置的通用模板 JAR
./gradlew jar -Ptemplate=true
```

### 产物位置：
`client/build/libs/modsync-neoforge-26.1.2-0.2.0.jar`

将编译生成的 JAR 复制至 `server/app/mod_templates/` 即可作为服务端生成模板使用。

---

## 2. 构建 Forge 1.20.1 客户端更新器模板

Forge 1.20.1 采用轻量精确依赖编译流程，无需耗费长时间运行完整的反编译任务。

### 依赖说明：
- 核心 Forge、FML 与第三方通用依赖库（共 8 个）将由构建脚本自动从公共 Maven 仓库拉取并缓存至 `client/.cache/libraries/`。
- Minecraft 1.20.1 SRG 客户端映射库（`client-1.20.1-*-srg.jar`）受 Mojang 最终用户协议约束无法公开分发，需由开发者或启动器环境提供。安装了 Minecraft 1.20.1 Forge 的任何启动器（如 PCL、HMCL、官方启动器）均已自动下载该库至 `.minecraft/libraries` 目录下。

### 构建步骤：

```bash
cd client
# 确保环境使用 JDK 17
export JAVA_HOME="/path/to/jdk-17"
export PATH="$JAVA_HOME/bin:$PATH"

# 执行构建并指定 libraries 所在目录
python build_forge_template.py --mc-libs "/path/to/.minecraft/libraries"
```

*(亦可设置环境变量 `export MC_LIBRARIES_DIR="/path/to/.minecraft/libraries"` 后直接运行 `python build_forge_template.py`)*

### 产物位置：
脚本构建完成后，将直接将纯净模板输出至：
`server/app/mod_templates/modsync-forge-1.20.1-0.2.0.jar`

---

## 3. 产物纯净度校验

无论通过何种方式构建模板，均可通过以下命令校验模板内部确实不包含任何真实 IP 或私有密钥：

```bash
python -c "
import zipfile
with zipfile.ZipFile('server/app/mod_templates/modsync-forge-1.20.1-0.2.0.jar') as z:
    cfg = z.read('modsync-server.json').decode('utf-8')
    print('Internal config:', cfg)
    assert 'http://' not in cfg
"
```
