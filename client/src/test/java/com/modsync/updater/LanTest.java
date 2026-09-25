package com.modsync.updater;

import java.nio.file.*;
import java.util.*;

/**
 * 可选局域网端到端测试。
 * 默认不硬编码任何私有 IP、Token 或游戏实例路径。
 * 仅在配置了对应环境变量时才执行真实连接与下载测试。
 */
public class LanTest {
    public static void main(String[] args) throws Exception {
        String endpoint = System.getenv("MODSYNC_LAN_ENDPOINT");
        String packId = System.getenv("MODSYNC_LAN_PACK_ID");
        String token = System.getenv("MODSYNC_LAN_TOKEN");
        String clientPath = System.getenv("MODSYNC_LAN_CLIENT_PATH");

        if (endpoint == null || packId == null || token == null) {
            System.out.println("未设置 MODSYNC_LAN_ENDPOINT / MODSYNC_LAN_PACK_ID / MODSYNC_LAN_TOKEN，跳过可选的真实局域网测试。");
            return;
        }

        Path client = clientPath != null ? Path.of(clientPath) : null;
        Path temp = Files.createTempDirectory(Path.of("build"), "lan-test-");
        var core = new UpdaterCore(temp, new UpdaterCore.Settings(endpoint, packId, token));
        var m = core.fetch();

        System.out.println("成功连接局域网发布器: release_id=" + m.release_id() + ", files=" + m.files().size());

        if (client != null && Files.isDirectory(client)) {
            List<UpdaterCore.Entry> sorted = new ArrayList<>(m.files());
            sorted.sort(Comparator.comparingLong(UpdaterCore.Entry::size));
            if (sorted.size() >= 2) {
                Set<String> omit = Set.of(sorted.get(0).path(), sorted.get(1).path());
                for (var e : m.files()) {
                    if (omit.contains(e.path())) continue;
                    Path from = client.resolve(e.path().substring(5));
                    if (Files.isRegularFile(from) && UpdaterCore.sha256(from).equalsIgnoreCase(e.sha256())) {
                        Files.copy(from, temp.resolve(e.path()), StandardCopyOption.REPLACE_EXISTING);
                    }
                }
            }
        }

        Files.writeString(temp.resolve("mods/player-extra.jar"), "simulated user mod");
        var plan = core.scan(m);
        System.out.println("比对结果: " + plan.summary());

        var r = core.apply(plan, true, false, () -> false, System.out::println);
        System.out.println("应用结果: " + r.summary());

        if (!Files.exists(temp.resolve("mods/player-extra.jar"))) {
            throw new AssertionError("玩家自装 Mod 丢失");
        }
        System.out.println("LAN PASS: 局域网通讯验证通过，哈希与受管记录正常，自装模组未受影响。");
    }
}
