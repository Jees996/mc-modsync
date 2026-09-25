# ModSync 开源复刻与多环境部署踩坑记录

本文档真实记录在异构环境（Ubuntu / Debian、不同 Minecraft 模组加载器、大体量整合包同步）下部署与复刻 ModSync 时遇到的典型技术问题、排查原因及项目修正方案。

---

## 问题一：Windows 环境导出脚本在 Linux 下报错 `env: $'bash\r': No such file or directory`

### 1. 发生阶段
从 Windows 环境克隆或通过 `git archive` 导出项目源码，传输至 Linux (Ubuntu 26.04) 服务器后执行 `./install.sh`。

### 2. 报错现象
```text
env: $'bash\r': No such file or directory
env: use -[v]S to pass options in shebang lines
```

### 3. 原因排查
Windows 下 Git 默认的换行符转换机制（`core.autocrlf`）将 Bash 脚本的行尾转换为了 Windows 格式（CRLF, `\r\n`）。Linux 内核在解析第一行 `#!/usr/bin/env bash\r` 时，将解释器路径识别为 `bash\r`，导致找不到二进制。

### 4. 项目修正
- 在仓库根目录配置 `.gitattributes`，显式固化脚本换行规则：
  ```gitattributes
  *.sh text eol=lf
  ```
- 将 `install.sh` 脚本文件格式永久转换为 Unix LF 编码。

### 5. 复测结果
在 Linux 环境重新执行 `./install.sh --yes`，脚本正常由系统 Bash 解析运行。

---

## 问题二：大型整合包批量同步时触发服务端频率限制（HTTP 429）

### 1. 发生阶段
客户端在同步包含 400+ 个模组的大型整合包（如香草纪元系列）时，下载进行至 100 余个文件后，后续文件批量失败。

### 2. 报错现象
客户端报出：
```text
Update result: 完成：下载 137，未完成 268 项。
HTTP Error: 429 Too Many Requests
```

### 3. 原因排查
- 服务端内置了单 IP 滑动窗口限流器（`ip_rate_limiter`）。初始默认 `RATE_LIMIT_RPS` 设为 30。
- 在千兆/万兆局域网环境下，客户端多文件单线程顺序拉取小文件（如几十 KB 的模组）耗时仅数毫秒，瞬间请求频率超过 50~100 次/秒，导致触发防刷保护被拦截。

### 4. 项目修正
- **客户端弹性重试**：在 `UpdaterCore.java` 的 `download` 逻辑中针对 HTTP 429 增加指数退避重试（Backoff & Retry，等待 400ms~1.2s，最多重试 3 次）。
- **服务端参数调整**：在 `deploy/.env.example` 与部署配置中将默认 `RATE_LIMIT_RPS` 提高至 60，并在局域网部署环境下根据实际需求放宽至 120。

### 5. 复测结果
在 425 个模组的真实测试沙箱中执行批量同步，所有文件全数顺利下载校验完成，无 429 报错中断。

---

## 问题三：Java 17 运行时兼容性与 Minecraft 1.20.1 GUI API 架构差异

### 1. 发生阶段
为经典版本 Minecraft 1.20.1 (Forge) 适配客户端模组时，编译与字节码运行报错。

### 2. 报错现象
- 初始代码中使用了 Java 21+ 的虚拟线程/平台线程 API：`Thread.ofPlatform()`，在 Java 17 目标下报错：`cannot find symbol`。
- Minecraft 26.1.2 使用全新的 `GuiGraphicsExtractor` 架构，而 1.20.1 采用 `GuiGraphics` + MCP/Searge 混淆命名体系。

### 3. 原因排查
- Minecraft 1.20.1 规范要求运行在 Java 17 上，不能使用 Java 21/25 的新特性。
- NeoForge 20.4+/26.1+ 与经典 Forge 1.20.1 在屏幕渲染与事件系统上存在代际差异：
  - 1.20.1 `Screen` 渲染方法为 `m_88315_(GuiGraphics, int, int, float)`；
  - 1.20.1 组件添加方法为 `m_142416_(Renderable & GuiEventListener)`；
  - 屏幕尺寸字段为 `f_96543_` (width) 与 `f_96544_` (height)。

### 4. 项目修正
- 重构 `UpdaterCore.java`，使用通用 `new Thread(...)`，全量消除 Java 21+ 依赖，严格保证代码在 Java 17+ 跨版本二进制兼容。
- 独立编写 `modsync-forge-1.20.1` 适配器，正确对接 Forge 1.20.1 的 Screen 与 GuiGraphics 渲染逻辑，并在 `templates.json` 中独立注册。

### 5. 复测结果
使用 OpenJDK 17.0.15 编译 Forge 1.20.1 模板 JAR，退出码为 0；在沙箱实例中顺利加载并完成版本检查与下载。

---

## 问题四：国内服务器 Docker 镜像源访问超时

### 1. 发生阶段
在未配置 Docker 镜像源的纯净 Ubuntu 主机上执行 `install.sh` 构建镜像时，拉取基础镜像 `python:3.12-slim` 超时。

### 2. 报错现象
```text
failed to do request: Head "https://registry-1.docker.io/v2/library/python/manifests/3.12-slim": dial tcp: i/o timeout
```

### 3. 原因排查
国内部分云主机或家庭局域网环境直接访问 Docker Hub 存在网络超时限制。

### 4. 解决建议
- 在 `/etc/docker/daemon.json` 中配置国内可用的加速镜像源（如 `registry-mirrors`），或配置代理；
- 亦可通过已有机器导出缓存镜像：`docker save python:3.12-slim | ssh <target> "docker load"` 直接离线导入。

---

## 问题五：Forge 1.20.1 模组加载器报 `Missing required field mandatory in dependency` 并忽略模组

### 1. 发生阶段
客户端放入生成的 `modsync-forge-1.20.1-client.jar` 后启动 Minecraft 1.20.1，主菜单未显示“整合包更新”按钮。

### 2. 报错现象
在客户端 `logs/debug.log` 中记录：
```text
net.minecraftforge.fml.loading.moddiscovery.InvalidModFileException: Missing required field mandatory in dependency (modsync-forge-1.20.1-client.jar)
[Render thread/WARN] [net.minecraftforge.client.loading.ClientModLoader/LOADING]: 文件modsync-forge-1.20.1-client.jar不是有效的Mod文件
```

### 3. 原因排查
- NeoForge 1.20.4+ / 26.x 模组元数据采用了 `type = "required"` 语法。
- 而经典 Forge 1.20.1 的 FML 加载器在解析 `[[dependencies.<modId>]]` 时，强制要求 `mandatory = true`（布尔值），且必须包含 `ordering` 与 `side`。若缺少 `mandatory` 字段，Forge 会直接抛出 `InvalidModFileException` 并将整个 JAR 标记为非有效模组跳过加载。

### 4. 项目修正
在 Forge 1.20.1 构建模版中修正 `META-INF/mods.toml` 依赖块定义：
```toml
[[dependencies.modsync]]
modId = "forge"
mandatory = true
versionRange = "[47.1.0,)"
ordering = "NONE"
side = "BOTH"

[[dependencies.modsync]]
modId = "minecraft"
mandatory = true
versionRange = "[1.20.1, 1.20.2)"
ordering = "NONE"
side = "BOTH"
```
同时添加 `displayTest = "IGNORE_ALL_VERSION"` 声明为纯客户端可选更新模组，免去服务端安装要求。

### 5. 复测结果
更新后的 JAR 放入香草纪元客户端 `mods` 目录，FML 顺利将其识别为合法有效模组。

---

## 问题六：发布目录混入更新器导致安全自锁，以及 1.20.1 GUI 文本误用居中绘制导致左侧被截断

### 1. 发生阶段
服主将客户端完整 `mods` 文件夹一次性复制到分发目录发布后，客户端点击“整合包更新”进入页面，报出 `发布清单包含更新器自身，请服主从发布目录移除更新器`；同时界面文字整体偏向左侧且左半边字符被屏幕边缘截断。

### 2. 报错现象
- 状态栏显示：`检查失败：发布清单包含更新器自身，请服主从发布目录移除更新器`
- 界面内所有说明文字从左边缘偏出，行首文字如 `未改`、`发布清单包含` 缺失看不见。

### 3. 原因排查
1. **服务端发布清单混入更新器**：管理员直接拷入现有整合包 `mods` 目录时，原有的更新器自身也被服务端 `scanner.py` 当作普通玩法 Mod 扫进清单。客户端更新器在比对清单时发现目标列表包含自身，触发防自我覆盖的安全保护而抛出异常。
2. **文本绘制误用居中对齐**：Minecraft 1.20.1 `GuiGraphics` 中 `m_280137_` 与 `m_280364_` 实质是 `drawCenteredString`（即 `x - font.width / 2`）。代码传入了 `x = 16`，导致绘制起始坐标为负数（如 `16 - 150 = -134`），文字前半截被画到了屏幕左侧外面。

### 4. 项目修正
1. **服务端自动过滤**：修改 `server/app/scanner.py`，扫描时自动排除以 `modsync` 与 `lee-updater` 开头的文件，禁止发布器将自身作为分发项纳入清单。
2. **客户端文本改为标准左对齐**：在 `UpdaterScreen.java` 中，将正文与状态行绘制切换为 `m_280488_` 与 `m_280648_`（即标准左对齐 `drawString`），文字从 `x = 16` 起向右绘制，边界完整对齐深色底框。

### 5. 复测结果
- NUC 重新扫描后发布新清单 `rel_20260925_080949_8c87`，更新器自身已不在清单中。
- 生成的新版更新器在界面上文字靠左对齐，完整清晰显示，不再被左边缘截断。
