package com.modsync.updater;

import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.gui.TextAlignment;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;
import net.minecraft.util.FormattedCharSequence;

public final class UpdaterScreen extends Screen {
    private final Screen parent;
    private UpdaterCore core;
    private UpdaterCore.Plan plan;
    private final AtomicBoolean cancel = new AtomicBoolean();
    private volatile boolean busy;
    private boolean overwrite = true;
    private boolean repairArmed = false;
    private String status = "点击【检查更新】连接发布器比对文件。";
    private String details = "说明：\n1. 普通更新保留自装 Mod，仅下载缺少项并替换更新项；\n2. 修复模式会恢复为服主发布的标准列表，并清理多余 Mod（更新器本体受保护）；\n3. 更新操作仅修改文件，需重启客户端后方可加载新模组。\n\n准备就绪后，请点击左下方【检查更新】。";
    private List<FormattedCharSequence> lines = List.of();
    private int page = 0;
    private Button check, update, repair, mode, closeGame;

    public UpdaterScreen(Screen parent) {
        super(Component.literal("整合包更新"));
        this.parent = parent;
    }

    @Override
    protected void init() {
        int left = Math.max(8, width / 2 - 150), y = height - 76;
        check = addRenderableWidget(Button.builder(Component.literal("检查更新"), b -> check()).bounds(left, y, 96, 20).build());
        update = addRenderableWidget(Button.builder(Component.literal("更新"), b -> apply(false)).bounds(left + 102, y, 96, 20).build());
        repair = addRenderableWidget(Button.builder(Component.literal(repairArmed ? "确认修复删除" : "修复"), b -> {
            if (!repairArmed) {
                repairArmed = true;
                b.setMessage(Component.literal("确认修复删除"));
                status = "警告：修复将删除下方列出的额外 Mod，再次点击此按钮确认执行；不备份。";
                showPlan();
            } else {
                apply(true);
            }
        }).bounds(left + 204, y, 96, 20).build());

        mode = addRenderableWidget(Button.builder(Component.literal(overwrite ? "同名不同：覆盖" : "同名不同：跳过"), b -> {
            overwrite = !overwrite;
            b.setMessage(Component.literal(overwrite ? "同名不同：覆盖" : "同名不同：跳过"));
        }).bounds(left, y + 24, 146, 20).build());

        closeGame = addRenderableWidget(Button.builder(Component.literal("关闭游戏"), b -> Minecraft.getInstance().stop()).bounds(left + 154, y + 24, 146, 20).build());

        addRenderableWidget(Button.builder(Component.literal("上页"), b -> { page = Math.max(0, page - 1); }).bounds(left, height - 26, 60, 20).build());
        addRenderableWidget(Button.builder(Component.literal("下页"), b -> { page = Math.min(maxPage(), page + 1); }).bounds(left + 66, height - 26, 60, 20).build());
        addRenderableWidget(Button.builder(Component.literal("取消 / 返回"), b -> onClose()).bounds(left + 154, height - 26, 146, 20).build());

        wrap();
        refresh();
        // 注意：严格遵循零自动联网原则，init() 内不自动发起任何网络请求
    }

    private int perPage() { return Math.max(1, (height - 150) / 12); }
    private int maxPage() { return Math.max(0, (lines.size() - 1) / perPage()); }
    private void wrap() {
        lines = font.split(Component.literal(details), Math.max(100, width - 36));
        page = Math.min(page, maxPage());
    }

    private void refresh() {
        if (check == null) return;
        check.active = !busy;
        update.active = !busy && plan != null;
        repair.active = !busy && plan != null;
        mode.active = !busy;
        closeGame.active = !busy;
    }

    private void work(Runnable task) {
        busy = true;
        cancel.set(false);
        refresh();
        Thread.ofPlatform().daemon(true).name("modsync-worker").start(() -> {
            try {
                task.run();
            } finally {
                Minecraft.getInstance().execute(() -> {
                    busy = false;
                    refresh();
                });
            }
        });
    }

    private void ui(Runnable task) { Minecraft.getInstance().execute(task); }

    private void check() {
        plan = null;
        repairArmed = false;
        if (repair != null) repair.setMessage(Component.literal("修复"));
        status = "正在连接并比对文件，请稍候……";
        work(() -> {
            try {
                if (core == null) {
                    core = new UpdaterCore(Minecraft.getInstance().gameDirectory.toPath(), UpdaterCore.Settings.embedded());
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
                    details = "未改动本地文件。\n请确认：\n1. 电脑与更新服务器处于同一网络；\n2. 发布器服务已启动；\n3. 客户端未被禁用下载。";
                    wrap();
                });
            }
        });
    }

    private void showPlan() {
        if (plan == null) return;
        StringBuilder s = new StringBuilder("发布版本：").append(plan.manifest().release_id()).append("\n\n")
                .append(Objects.requireNonNullElse(plan.manifest().notes(), "")).append("\n\n【本地比对差异】\n");

        if (plan.missing().isEmpty() && plan.changed().isEmpty() && plan.removed().isEmpty()) {
            s.append("本地与当前发布列表完全一致，无需更新。\n");
        } else {
            plan.missing().forEach(e -> s.append("+ 缺少 ").append(e.path()).append('\n'));
            plan.changed().forEach(e -> s.append("~ 不同 ").append(e.path()).append('\n'));
            plan.removed().forEach(e -> s.append("- 旧受管 ").append(e).append('\n'));
        }

        if (repairArmed) {
            s.append("\n【修复将直接删除以下额外Mod】：\n");
            if (plan.extras().isEmpty()) {
                s.append("（无额外 Mod 需要删除）\n");
            } else {
                plan.extras().forEach(e -> s.append("- ").append(e).append('\n'));
            }
        } else {
            s.append("\n自装/未知Mod：").append(plan.extras().size()).append(" 个（普通更新将予以保留）。");
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
                    if (repair != null) repair.setMessage(Component.literal("修复"));
                    status = result.summary();
                    details = result.summary() + "\n\n"
                            + (result.failures().isEmpty() ? "全部操作已成功完成。" : "未完成的项目：\n" + String.join("\n", result.failures()))
                            + "\n\n提示：当前游戏仍运行旧代码。请点击下方“关闭游戏”按钮正常退出，再从启动器重新启动即可生效。\n若提示文件占用失败，请关闭游戏后再试。";
                    page = 0;
                    wrap();
                });
            } catch (Exception e) {
                ui(() -> {
                    plan = null;
                    status = "未全部完成：" + UpdaterCore.friendly(e);
                    details = status + "\n已成功下载并校验的文件保留在本地；请重新检查。";
                    wrap();
                });
            }
        });
    }

    @Override
    public void onClose() {
        if (busy) {
            cancel.set(true);
            status = "正在取消操作，请等待当前网络请求结束……";
            return;
        }
        Minecraft.getInstance().setScreen(parent);
    }

    @Override
    public void extractRenderState(GuiGraphicsExtractor g, int mouseX, int mouseY, float partialTick) {
        super.extractRenderState(g, mouseX, mouseY, partialTick);
        g.fill(10, 38, width - 10, height - 82, 0x88000000);
        g.textRenderer().accept(TextAlignment.CENTER, width / 2, 10, title);
        g.textRenderer().accept(TextAlignment.LEFT, 14, 25, Component.literal(font.plainSubstrByWidth(status, Math.max(100, width - 28))));
        int from = page * perPage(), end = Math.min(lines.size(), from + perPage());
        for (int i = from; i < end; i++) {
            g.textRenderer().accept(TextAlignment.LEFT, 16, 44 + (i - from) * 12, lines.get(i));
        }
        g.textRenderer().accept(TextAlignment.RIGHT, width - 14, height - 95, Component.literal((page + 1) + " / " + (maxPage() + 1)));
    }
}
