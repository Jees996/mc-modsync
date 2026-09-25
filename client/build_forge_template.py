import subprocess, tempfile, os, zipfile, hashlib
from pathlib import Path

# Paths
script_dir = Path(__file__).resolve().parent
repo_root = script_dir.parent

# JDK javac path (can be configured via JAVA_HOME or system PATH)
user_home = Path.home()
default_javac = user_home / 'AppData/Roaming/.minecraft/runtime/java-runtime-gamma-snapshot/bin/javac.exe'
java_home = os.environ.get('JAVA_HOME')

if java_home and (Path(java_home) / 'bin/javac.exe').exists():
    javac = (Path(java_home) / 'bin/javac.exe').as_posix()
elif java_home and (Path(java_home) / 'bin/javac').exists():
    javac = (Path(java_home) / 'bin/javac').as_posix()
elif default_javac.exists():
    javac = default_javac.as_posix()
else:
    javac = 'javac'

# Collect Minecraft libraries from environment or standard paths
mc_libs_dir = os.environ.get('MC_LIBRARIES_DIR')
candidate_lib_dirs = [
    Path(mc_libs_dir) if mc_libs_dir else None,
    user_home / 'AppData/Roaming/.minecraft/libraries',
    Path('E:/Minecraft/.minecraft/libraries')
]

libs_path = None
for c in candidate_lib_dirs:
    if c and c.exists():
        libs_path = c
        break

libs = []
srg_jar = None
if libs_path:
    for p in libs_path.rglob('*.jar'):
        libs.append(p.as_posix())
        if 'client-1.20.1' in p.name and 'srg' in p.name:
            srg_jar = p

if srg_jar:
    libs.append(srg_jar.as_posix())

updater_core_src = (script_dir / 'src/main/java/com/modsync/updater/UpdaterCore.java').read_text(encoding='utf-8')

updater_mod_src = """package com.modsync.updater;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.client.gui.screens.multiplayer.JoinMultiplayerScreen;
import net.minecraft.network.chat.Component;
import net.minecraftforge.client.event.ScreenEvent;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.fml.common.Mod;

@Mod("modsync")
public final class UpdaterMod {
    public UpdaterMod() {
        MinecraftForge.EVENT_BUS.addListener(UpdaterMod::onScreenInit);
    }

    private static void onScreenInit(ScreenEvent.Init.Post event) {
        if (event.getScreen() instanceof TitleScreen screen) {
            event.addListener(Button.m_253074_(Component.m_237113_("整合包更新"), button ->
                    Minecraft.m_91087_().m_91152_(new UpdaterScreen(screen)))
                    .m_252987_(Math.max(4, screen.f_96543_ - 110), Math.max(4, screen.f_96544_ - 48), 104, 20)
                    .m_253136_());
        } else if (event.getScreen() instanceof JoinMultiplayerScreen screen) {
            event.addListener(Button.m_253074_(Component.m_237113_("§e§l【更新提示】§f进服提示模组版本不对？点此检查更新"), button ->
                    Minecraft.m_91087_().m_91152_(new UpdaterScreen(screen)))
                    .m_252987_(Math.max(4, screen.f_96543_ / 2 - 180), 8, 360, 20)
                    .m_253136_());
        }
    }
}
"""

updater_screen_src = """package com.modsync.updater;

import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;
import net.minecraft.util.FormattedCharSequence;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.api.distmarker.OnlyIn;

@OnlyIn(Dist.CLIENT)
public final class UpdaterScreen extends Screen {
    private final Screen parent;
    private UpdaterCore core;
    private UpdaterCore.Plan plan;
    private final AtomicBoolean cancel = new AtomicBoolean();
    private volatile boolean busy;
    private boolean overwrite = true;
    private boolean repairArmed = false;
    private String status = "点击【检查更新】连接发布器比对文件。";
    private String details = "说明：\\n1. 普通更新保留自装 Mod，仅下载缺少项并替换更新项；\\n2. 修复模式会恢复为服主发布的标准列表，并清理多余 Mod（更新器本体受保护）；\\n3. 更新操作仅修改文件，需重启客户端后方可加载新模组。\\n\\n准备就绪后，请点击左下方【检查更新】。";
    private List<FormattedCharSequence> lines = List.of();
    private int page = 0;
    private Button check, update, repair, mode, closeGame;

    public UpdaterScreen(Screen parent) {
        super(Component.m_237113_("整合包更新"));
        this.parent = parent;
    }

    @Override
    protected void m_7856_() {
        int left = Math.max(8, this.f_96543_ / 2 - 150), y = this.f_96544_ - 76;
        check = this.m_142416_(Button.m_253074_(Component.m_237113_("检查更新"), b -> check()).m_252987_(left, y, 96, 20).m_253136_());
        update = this.m_142416_(Button.m_253074_(Component.m_237113_("更新"), b -> apply(false)).m_252987_(left + 102, y, 96, 20).m_253136_());
        repair = this.m_142416_(Button.m_253074_(Component.m_237113_(repairArmed ? "确认修复删除" : "修复"), b -> {
            if (!repairArmed) {
                repairArmed = true;
                b.m_93666_(Component.m_237113_("确认修复删除"));
                status = "警告：修复将删除下方列出的额外 Mod，再次点击此按钮确认执行；不备份。";
                showPlan();
            } else {
                apply(true);
            }
        }).m_252987_(left + 204, y, 96, 20).m_253136_());

        mode = this.m_142416_(Button.m_253074_(Component.m_237113_(overwrite ? "同名不同：覆盖" : "同名不同：跳过"), b -> {
            overwrite = !overwrite;
            b.m_93666_(Component.m_237113_(overwrite ? "同名不同：覆盖" : "同名不同：跳过"));
        }).m_252987_(left, y + 24, 146, 20).m_253136_());

        closeGame = this.m_142416_(Button.m_253074_(Component.m_237113_("关闭游戏"), b -> Minecraft.m_91087_().m_91399_()).m_252987_(left + 154, y + 24, 146, 20).m_253136_());

        this.m_142416_(Button.m_253074_(Component.m_237113_("上页"), b -> { page = Math.max(0, page - 1); }).m_252987_(left, this.f_96544_ - 26, 60, 20).m_253136_());
        this.m_142416_(Button.m_253074_(Component.m_237113_("下页"), b -> { page = Math.min(maxPage(), page + 1); }).m_252987_(left + 66, this.f_96544_ - 26, 60, 20).m_253136_());
        this.m_142416_(Button.m_253074_(Component.m_237113_("取消 / 返回"), b -> m_7379_()).m_252987_(left + 154, this.f_96544_ - 26, 146, 20).m_253136_());

        wrap();
        refresh();
    }

    private int perPage() { return Math.max(1, (this.f_96544_ - 150) / 12); }
    private int maxPage() { return Math.max(0, (lines.size() - 1) / perPage()); }
    private void wrap() {
        lines = this.f_96547_.m_92923_(Component.m_237113_(details), Math.max(100, this.f_96543_ - 36));
        page = Math.min(page, maxPage());
    }

    private void refresh() {
        if (check == null) return;
        check.f_93623_ = !busy;
        update.f_93623_ = !busy && plan != null;
        repair.f_93623_ = !busy && plan != null;
        mode.f_93623_ = !busy;
        closeGame.f_93623_ = !busy;
    }

    private void work(Runnable task) {
        busy = true;
        cancel.set(false);
        refresh();
        Thread t = new Thread(() -> {
            try {
                task.run();
            } finally {
                Minecraft.m_91087_().execute(() -> {
                    busy = false;
                    refresh();
                });
            }
        }, "modsync-worker");
        t.setDaemon(true);
        t.start();
    }

    private void ui(Runnable task) { Minecraft.m_91087_().execute(task); }

    private void check() {
        plan = null;
        repairArmed = false;
        if (repair != null) repair.m_93666_(Component.m_237113_("修复"));
        status = "正在连接并比对文件，请稍候……";
        work(() -> {
            try {
                if (core == null) {
                    core = new UpdaterCore(Minecraft.m_91087_().f_91069_.toPath(), UpdaterCore.Settings.embedded());
                }
                var found = core.scan(core.fetch());
                ui(() -> {
                    plan = found;
                    status = found.summary();
                    showPlan();
                });
            } catch (Exception e) {
                ui(() -> {
                    status = "检查失败：" + UpdaterCore.friendly(e);
                    details = "未改动本地文件。\\n请确认：\\n1. 电脑与更新服务器处于同一网络；\\n2. 发布器服务已启动；\\n3. 客户端未被禁用下载。";
                    wrap();
                });
            }
        });
    }

    private void showPlan() {
        if (plan == null) return;
        StringBuilder s = new StringBuilder("发布版本：").append(plan.manifest().release_id()).append("\\n\\n")
                .append(Objects.requireNonNullElse(plan.manifest().notes(), "")).append("\\n\\n【本地比对差异】\\n");

        if (plan.missing().isEmpty() && plan.changed().isEmpty() && plan.removed().isEmpty()) {
            s.append("本地与当前发布列表完全一致，无需更新。\\n");
        } else {
            plan.missing().forEach(e -> s.append("+ 缺少 ").append(e.path()).append('\\n'));
            plan.changed().forEach(e -> s.append("~ 不同 ").append(e.path()).append('\\n'));
            plan.removed().forEach(e -> s.append("- 旧受管 ").append(e).append('\\n'));
        }

        if (repairArmed) {
            s.append("\\n【修复将直接删除以下额外Mod】：\\n");
            if (plan.extras().isEmpty()) {
                s.append("（无额外 Mod 需要删除）\\n");
            } else {
                plan.extras().forEach(e -> s.append("- ").append(e).append('\\n'));
            }
        } else {
            s.append("\\n自装/未知Mod：").append(plan.extras().size()).append(" 个（普通更新将予以保留）。");
        }
        details = s.toString();
        page = 0;
        wrap();
    }

    private void apply(boolean repairing) {
        if (plan == null || busy) return;
        var selected = plan;
        boolean replace = overwrite;
        status = "开始执行更新……";
        work(() -> {
            try {
                var result = core.apply(selected, replace, repairing, cancel::get, message -> ui(() -> status = message));
                ui(() -> {
                    plan = null;
                    repairArmed = false;
                    if (repair != null) repair.m_93666_(Component.m_237113_("修复"));
                    status = result.summary();
                    details = result.summary() + "\\n\\n"
                            + (result.failures().isEmpty() ? "全部操作已成功完成。" : "未完成的项目：\\n" + String.join("\\n", result.failures()))
                            + "\\n\\n提示：当前游戏仍运行旧代码。请点击下方“关闭游戏”按钮正常退出，再从启动器重新启动即可生效。\\n若提示文件占用失败，请关闭游戏后再试。";
                    page = 0;
                    wrap();
                });
            } catch (Exception e) {
                ui(() -> {
                    plan = null;
                    status = "未全部完成：" + UpdaterCore.friendly(e);
                    details = status + "\\n已成功下载并校验的文件保留在本地；请重新检查。";
                    wrap();
                });
            }
        });
    }

    @Override
    public void m_7379_() {
        if (busy) {
            cancel.set(true);
            status = "正在取消操作，请等待当前网络请求结束……";
            return;
        }
        Minecraft.m_91087_().m_91152_(parent);
    }

    @Override
    public void m_88315_(GuiGraphics g, int mouseX, int mouseY, float partialTick) {
        this.m_280273_(g);
        super.m_88315_(g, mouseX, mouseY, partialTick);
        g.m_280509_(10, 38, this.f_96543_ - 10, this.f_96544_ - 82, 0x88000000);
        g.m_280653_(this.f_96547_, this.f_96539_, this.f_96543_ / 2, 10, 0xFFFFFF);
        g.m_280488_(this.f_96547_, this.f_96547_.m_92834_(status, Math.max(100, this.f_96543_ - 32)), 16, 25, 0xFFFFFF);
        int from = page * perPage(), end = Math.min(lines.size(), from + perPage());
        for (int i = from; i < end; i++) {
            g.m_280648_(this.f_96547_, lines.get(i), 16, 44 + (i - from) * 12, 0xFFFFFF);
        }
        String pageStr = (page + 1) + " / " + (maxPage() + 1);
        g.m_280488_(this.f_96547_, pageStr, this.f_96543_ - 16 - this.f_96547_.m_92895_(pageStr), this.f_96544_ - 95, 0x888888);
    }
}
"""

mods_toml = """modLoader="javafml"
loaderVersion="[47,)"
license="MIT"

[[mods]]
modId="modsync"
version="0.2.0"
displayName="ModSync · 客户端更新"
description='''轻量 Minecraft 客户端模组同步更新器。'''
displayTest="IGNORE_ALL_VERSION"

[[dependencies.modsync]]
modId="forge"
mandatory=true
versionRange="[47.1.0,)"
ordering="NONE"
side="BOTH"

[[dependencies.modsync]]
modId="minecraft"
mandatory=true
versionRange="[1.20.1, 1.20.2)"
ordering="NONE"
side="BOTH"
"""

zh_cn_json = """{
  "modmenu.nameTranslation.modsync": "ModSync · 客户端更新"
}
"""

server_json = """{
  "endpoint": "",
  "packId": "",
  "token": ""
}
"""

manifest_mf = """Manifest-Version: 1.0
Implementation-Title: ModSync Forge 1.20.1 Client
Implementation-Version: 0.2.0
Specification-Title: ModSync
Specification-Version: 0.2.0
"""

def build():
    with tempfile.TemporaryDirectory() as td:
        src_dir = Path(td) / "src" / "com" / "modsync" / "updater"
        src_dir.mkdir(parents=True)
        out_classes = Path(td) / "classes"
        out_classes.mkdir()

        (src_dir / "UpdaterCore.java").write_text(updater_core_src, encoding="utf-8")
        (src_dir / "UpdaterMod.java").write_text(updater_mod_src, encoding="utf-8")
        (src_dir / "UpdaterScreen.java").write_text(updater_screen_src, encoding="utf-8")

        argfile = Path(td) / "javac_args.txt"
        arg_lines = [
            "-cp",
            ";".join(libs),
            "-d",
            out_classes.as_posix(),
            "-source",
            "17",
            "-target",
            "17",
            "-encoding",
            "utf-8"
        ] + [p.as_posix() for p in src_dir.glob("*.java")]
        argfile.write_text("\n".join(arg_lines) + "\n", encoding="utf-8")

        cmd = [javac, f"@{argfile.as_posix()}"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print("Compilation failed:", res.stderr)
            return False

        target_dir = repo_root / 'server/app/mod_templates'
        target_dir.mkdir(parents=True, exist_ok=True)
        out_jar = target_dir / 'modsync-forge-1.20.1-0.2.0.jar'

        with zipfile.ZipFile(out_jar, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('META-INF/MANIFEST.MF', manifest_mf)
            z.writestr('META-INF/mods.toml', mods_toml)
            z.writestr('assets/modsync/lang/zh_cn.json', zh_cn_json)
            z.writestr('modsync-server.json', server_json)

            for root, dirs, files in os.walk(out_classes):
                for f in files:
                    full_p = Path(root) / f
                    rel_p = full_p.relative_to(out_classes).as_posix()
                    z.write(full_p, rel_p)

        sha256 = hashlib.sha256(out_jar.read_bytes()).hexdigest()
        print(f"SUCCESS: Built {out_jar.name}")
        print(f"Path: {out_jar.as_posix()}")
        print(f"Size: {out_jar.stat().st_size} bytes")
        print(f"SHA-256: {sha256}")
        return True

if __name__ == '__main__':
    build()
