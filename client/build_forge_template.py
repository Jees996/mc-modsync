#!/usr/bin/env python3
"""
ModSync - Forge 1.20.1 客户端通用更新器模板构建脚本

设计说明：
- 编译真实 Java 源文件 (不包含任何内嵌硬编码源码字符串)
- 使用精确的 6 个核心依赖库 (不盲目全盘扫描未知 JAR)
- 5 个通用公共依赖自动从官方 Maven (MinecraftForge / Maven Central) 获取并缓存
- Minecraft 1.20.1 SRG 客户端库通过标准路径、参数 (--mc-libs) 或环境变量 (MC_LIBRARIES_DIR) 提供
- 若缺少必要依赖，输出清晰友好的定位说明，消除对特定机器路径的隐含依赖
"""

import argparse
import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CACHE_DIR = SCRIPT_DIR / ".cache" / "libraries"

# 公共标准依赖 (可直接从官方 Maven 仓库拉取并缓存)
MAVEN_DEPENDENCIES = [
    {
        "filename": "forge-1.20.1-47.3.33-universal.jar",
        "url": "https://maven.minecraftforge.net/net/minecraftforge/forge/1.20.1-47.3.33/forge-1.20.1-47.3.33-universal.jar",
        "sha256": "3cb49a4059062eb142e0ba3a22830f8fe697f394c8b21baaeeb3a595cb6cf4cb"
    },
    {
        "filename": "fmlcore-1.20.1-47.3.33.jar",
        "url": "https://maven.minecraftforge.net/net/minecraftforge/fmlcore/1.20.1-47.3.33/fmlcore-1.20.1-47.3.33.jar",
        "sha256": "47a3e74cfa7d10b7db8a883733075c3fce4206584c688c27932c5e5eeea89ce5"
    },
    {
        "filename": "forgespi-7.0.1.jar",
        "url": "https://maven.minecraftforge.net/net/minecraftforge/forgespi/7.0.1/forgespi-7.0.1.jar",
        "sha256": "82dc76bfd739c3621434190c10ee08c105553e20ec42ef3ff36665b1695ae535"
    },
    {
        "filename": "eventbus-6.0.5.jar",
        "url": "https://maven.minecraftforge.net/net/minecraftforge/eventbus/6.0.5/eventbus-6.0.5.jar",
        "sha256": "631cb1c7f4625b18cebe255f00e5ce28608e9860b0ec8cb9ebf2c8efab6bc216"
    },
    {
        "filename": "javafmllanguage-1.20.1-47.3.33.jar",
        "url": "https://maven.minecraftforge.net/net/minecraftforge/javafmllanguage/1.20.1-47.3.33/javafmllanguage-1.20.1-47.3.33.jar",
        "sha256": "038bfd46dbfe3910c66289b4b0825f385c5df39c1b3531b4028308dc6fe8276f"
    },
    {
        "filename": "mergetool-1.1.5-api.jar",
        "url": "https://maven.minecraftforge.net/net/minecraftforge/mergetool/1.1.5/mergetool-1.1.5-api.jar",
        "sha256": "4fc7feae961e680287ff1b17e4f8841496bfa75851d95c10fa2e8964d5dbf7ee"
    },
    {
        "filename": "brigadier-1.0.18.jar",
        "url": "https://libraries.minecraft.net/com/mojang/brigadier/1.0.18/brigadier-1.0.18.jar",
        "sha256": "8d394235fb342b4125b2901dbd3c9074a383d47ad9acdb0df2b8f88ceb95cb2e"
    },
    {
        "filename": "gson-2.10.1.jar",
        "url": "https://repo1.maven.org/maven2/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar",
        "sha256": "b064375e2ad01eb7fe44a54c8c738e469796e6d1c9e05f639c063cf4eb41cc63"
    }
]

def find_javac() -> str:
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / ("javac.exe" if platform.system() == "Windows" else "javac")
        if candidate.is_file():
            return candidate.as_posix()

    user_home = Path.home()
    standard_jdks = [
        user_home / "AppData/Roaming/.minecraft/runtime/java-runtime-gamma-snapshot/bin/javac.exe",
        user_home / "AppData/Roaming/.minecraft/runtime/java-runtime-gamma/bin/javac.exe",
        user_home / "AppData/Roaming/.minecraft/runtime/java-runtime-epsilon/bin/javac.exe"
    ]
    for jdk in standard_jdks:
        if jdk.is_file():
            return jdk.as_posix()

    return "javac"

def download_maven_dependencies() -> list[str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for dep in MAVEN_DEPENDENCIES:
        target = CACHE_DIR / dep["filename"]
        if not target.is_file() or target.stat().st_size == 0:
            print(f"[*] 正在从 Maven 下载依赖: {dep['filename']}...")
            try:
                req = urllib.request.Request(dep["url"], headers={"User-Agent": "Mozilla/5.0 (ModSync-Builder)"})
                with urllib.request.urlopen(req, timeout=30) as resp, open(target, "wb") as f:
                    f.write(resp.read())
            except Exception as e:
                print(f"[!] 从 Maven 仓库下载 {dep['filename']} 失败: {e}", file=sys.stderr)
                if not target.is_file():
                    sys.exit(1)
        paths.append(target.as_posix())
    return paths

def locate_client_srg(custom_libs_dir: str | None = None) -> str | None:
    # 候选路径检查（消除对特定作者机器的写死依赖，按标准启动器与参数查找）
    candidates = []
    if custom_libs_dir:
        candidates.append(Path(custom_libs_dir))
    if os.environ.get("MC_LIBRARIES_DIR"):
        candidates.append(Path(os.environ["MC_LIBRARIES_DIR"]))
    if os.environ.get("MC_HOME"):
        candidates.append(Path(os.environ["MC_HOME"]) / "libraries")

    user_home = Path.home()
    if platform.system() == "Windows":
        candidates.append(user_home / "AppData/Roaming/.minecraft/libraries")
    elif platform.system() == "Darwin":
        candidates.append(user_home / "Library/Application Support/minecraft/libraries")
    else:
        candidates.append(user_home / ".minecraft/libraries")

    target_name_part = "client-1.20.1"
    for base in candidates:
        if base and base.is_dir():
            srg_path = base / "net/minecraft/client/1.20.1-20230612.114412/client-1.20.1-20230612.114412-srg.jar"
            if srg_path.is_file():
                return srg_path.as_posix()
            for p in base.glob(f"**/*{target_name_part}*srg*.jar"):
                if p.is_file():
                    return p.as_posix()

    # 兜底：如果本地缓存已有该文件
    cached_srg = CACHE_DIR / "client-1.20.1-srg.jar"
    if cached_srg.is_file():
        return cached_srg.as_posix()

    return None

def main():
    parser = argparse.ArgumentParser(description="ModSync Forge 1.20.1 客户端通用更新器模板构建工具")
    parser.add_argument("--mc-libs", dest="mc_libs", help="指定 Minecraft 运行库所在目录 (例如 .minecraft/libraries)")
    parser.add_argument("--output", dest="output", help="输出的通用模板 JAR 路径")
    args = parser.parse_args()

    javac = find_javac()
    print(f"[*] 使用 JDK 编译器: {javac}")

    # 1. 准备 Maven 核心依赖库
    classpath_jars = download_maven_dependencies()

    # 2. 定位 client-1.20.1-srg.jar
    srg_jar = locate_client_srg(args.mc_libs)
    if not srg_jar:
        print("\n" + "=" * 60, file=sys.stderr)
        print("[错误] 未找到 Minecraft 1.20.1 客户端映射库 (client-1.20.1-*-srg.jar)！", file=sys.stderr)
        print("说明：由于 Minecraft 客户端二进制受 Mojang 最终用户协议保护，无法预打包分发。", file=sys.stderr)
        print("请通过以下任意方式提供该运行库所在目录：", file=sys.stderr)
        print("  1. 运行参数：python build_forge_template.py --mc-libs <.minecraft/libraries 路径>", file=sys.stderr)
        print("  2. 环境变量：export MC_LIBRARIES_DIR=\"/path/to/.minecraft/libraries\"", file=sys.stderr)
        print("  3. 或将 client-1.20.1-srg.jar 直接复制至 client/.cache/libraries/", file=sys.stderr)
        print("=" * 60 + "\n", file=sys.stderr)
        sys.exit(1)

    print(f"[+] 找到 Minecraft 1.20.1 SRG 客户端库: {srg_jar}")
    classpath_jars.append(srg_jar)

    # 3. 收集并检查源文件
    core_src = SCRIPT_DIR / "src/main/java/com/modsync/updater/UpdaterCore.java"
    forge_dir = SCRIPT_DIR / "forge-1.20.1"
    mod_src = forge_dir / "src/main/java/com/modsync/updater/UpdaterMod.java"
    screen_src = forge_dir / "src/main/java/com/modsync/updater/UpdaterScreen.java"
    mods_toml = forge_dir / "src/main/resources/META-INF/mods.toml"
    zh_cn_json = forge_dir / "src/main/resources/assets/modsync/lang/zh_cn.json"
    pack_mcmeta = forge_dir / "src/main/resources/pack.mcmeta"

    required_sources = [core_src, mod_src, screen_src, mods_toml, zh_cn_json, pack_mcmeta]
    for s in required_sources:
        if not s.is_file():
            print(f"[!] 缺失源码或资源文件: {s.as_posix()}", file=sys.stderr)
            sys.exit(1)

    # 4. 执行干净编译
    with tempfile.TemporaryDirectory() as td:
        out_classes = Path(td) / "classes"
        out_classes.mkdir()

        java_files = [core_src.as_posix(), mod_src.as_posix(), screen_src.as_posix()]
        argfile = Path(td) / "javac_args.txt"
        arg_lines = [
            "-cp",
            ";".join(classpath_jars) if platform.system() == "Windows" else ":".join(classpath_jars),
            "-d",
            out_classes.as_posix(),
            "-source",
            "17",
            "-target",
            "17",
            "-encoding",
            "utf-8"
        ] + java_files

        argfile.write_text("\n".join(arg_lines) + "\n", encoding="utf-8")

        print("[*] 正在编译 Forge 1.20.1 源码 (Java 17 字节码)...")
        cmd = [javac, f"@{argfile.as_posix()}"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print("[!] 编译失败:", res.stderr, file=sys.stderr)
            sys.exit(1)

        # 5. 打包生成纯净通用模板 JAR
        if args.output:
            out_jar = Path(args.output).resolve()
        else:
            out_jar = REPO_ROOT / "server/app/mod_templates/modsync-forge-1.20.1-0.2.0.jar"

        out_jar.parent.mkdir(parents=True, exist_ok=True)

        manifest_mf = (
            "Manifest-Version: 1.0\r\n"
            "Implementation-Title: ModSync Forge 1.20.1 Client\r\n"
            "Implementation-Version: 0.2.0\r\n"
            "Specification-Title: ModSync\r\n"
            "Specification-Version: 0.2.0\r\n\r\n"
        )
        server_json = '{\n  "endpoint": "",\n  "packId": "",\n  "token": ""\n}\n'

        with zipfile.ZipFile(out_jar, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("META-INF/MANIFEST.MF", manifest_mf)
            z.write(mods_toml, "META-INF/mods.toml")
            z.write(zh_cn_json, "assets/modsync/lang/zh_cn.json")
            z.write(pack_mcmeta, "pack.mcmeta")
            z.writestr("modsync-server.json", server_json)

            for root, _, files in os.walk(out_classes):
                for f in files:
                    full_p = Path(root) / f
                    rel_p = full_p.relative_to(out_classes).as_posix()
                    z.write(full_p, rel_p)

        sha256 = hashlib.sha256(out_jar.read_bytes()).hexdigest()
        print("=" * 60)
        print(f"[+] 成功构建通用模板: {out_jar.name}")
        print(f"    输出路径: {out_jar.as_posix()}")
        print(f"    文件大小: {out_jar.stat().st_size} 字节")
        print(f"    SHA-256 : {sha256}")

        templates_file = out_jar.parent / "templates.json"
        if templates_file.is_file() and not args.output:
            try:
                import json
                with open(templates_file, "r", encoding="utf-8") as tf:
                    t_data = json.load(tf)
                updated = False
                for t in t_data:
                    if t.get("id") == "forge-1.20.1":
                        t["sha256"] = sha256
                        updated = True
                if updated:
                    with open(templates_file, "w", encoding="utf-8") as tf:
                        json.dump(t_data, tf, indent=2, ensure_ascii=False)
                        tf.write("\n")
                    print(f"[+] 已同步更新模板清单 sha256: {templates_file.as_posix()}")
            except Exception as e:
                print(f"[!] 同步更新 templates.json 失败: {e}", file=sys.stderr)

        print("=" * 60)

if __name__ == "__main__":
    main()
