package com.modsync.updater;

import com.google.gson.*;
import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;
import java.util.function.*;
import static java.nio.file.LinkOption.NOFOLLOW_LINKS;

/**
 * 客户端核心逻辑：网络通讯、版本比对、文件下载与本地文件操作。
 * 无 Minecraft 运行时依赖，便于脱机独立测试。
 */
public final class UpdaterCore {
    static final Gson JSON = new GsonBuilder().setPrettyPrinting().create();

    public record Settings(String endpoint, String packId, String token) {
        public static Settings embedded() throws IOException {
            InputStream stream = UpdaterCore.class.getResourceAsStream("/modsync-server.json");
            if (stream == null) {
                stream = UpdaterCore.class.getResourceAsStream("/lee-updater-server.json");
            }
            if (stream == null) {
                throw new IOException("缺少内置服务器配置 (modsync-server.json)");
            }
            try (InputStream in = stream) {
                String raw = new String(in.readAllBytes(), StandardCharsets.UTF_8).replace("﻿", "");
                Settings s = JSON.fromJson(raw, Settings.class);
                if (s == null || s.endpoint() == null || s.endpoint().isBlank()
                        || s.endpoint().contains("example.invalid") || s.token() == null || s.token().isBlank()) {
                    throw new IOException("未配置有效的服务器连接信息。请使用发布器后台一键生成，或配置 modsync-server.json。");
                }
                return s;
            }
        }
    }

    public record Entry(String id, String path, long size, String sha256, String download_path) {}
    public record Manifest(int schema_version, String pack_id, String release_id, String notes, List<Entry> files) {}

    public record Plan(Manifest manifest, List<Entry> missing, List<Entry> changed, List<String> removed,
                       List<String> extras, Map<String, String> managed, int matching) {
        public String summary() {
            return "缺少 " + missing.size() + " · 覆盖 " + changed.size() + " · 旧版清理 " + removed.size()
                    + " · 自装/未知 " + extras.size() + " · 一致 " + matching;
        }
    }

    public record Result(int downloaded, int deleted, int skipped, List<String> failures) {
        public String summary() {
            return "完成：下载 " + downloaded + "，删除 " + deleted + "，跳过 " + skipped
                    + (failures.isEmpty() ? "。变更需重启游戏生效。" : "，未完成 " + failures.size() + " 项。");
        }
    }

    private final Settings settings;
    private final Path mods, state;
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();

    public UpdaterCore(Path instance, Settings settings) throws IOException {
        this.settings = settings;
        URI uri = URI.create(settings.endpoint());
        if ((!"http".equals(uri.getScheme()) && !"https".equals(uri.getScheme())) || uri.getHost() == null
                || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null) {
            throw new IOException("更新服务器地址格式无效");
        }

        this.mods = instance.resolve("mods");
        this.state = instance.resolve(".modsync");

        if (Files.isSymbolicLink(mods) || Files.isSymbolicLink(state)) {
            throw new IOException("安全限制：拒绝使用符号链接目录");
        }

        Files.createDirectories(mods);
        Files.createDirectories(state);

        // 迁移旧版受管记录（如果存在 .lee-updater 但尚未初始化 .modsync）
        Path legacyState = instance.resolve(".lee-updater");
        Path legacyManaged = legacyState.resolve("managed.json");
        Path currentManaged = state.resolve("managed.json");
        if (!Files.exists(currentManaged) && Files.exists(legacyManaged)) {
            try {
                Files.copy(legacyManaged, currentManaged, StandardCopyOption.REPLACE_EXISTING);
            } catch (Exception ignored) {
            }
        }
    }

    private HttpRequest request(String path) {
        return HttpRequest.newBuilder(URI.create(settings.endpoint().replaceAll("/+$", "") + path))
                .timeout(Duration.ofMinutes(3))
                .header("Authorization", "Bearer " + settings.token())
                .GET()
                .build();
    }

    private static IOException httpError(int code) {
        return new IOException(switch (code) {
            case 401, 403 -> "客户端下载口令无效或未授权（" + code + "）";
            case 404 -> "尚未发布整合包或文件不存在（404）";
            case 409, 410 -> "源文件已变动，请联系服主重新在后台发布（" + code + "）";
            case 429 -> "请求频率过高，请稍后重试（429）";
            case 503 -> "更新下载服务当前已停用维护中（503）";
            default -> "更新服务器响应异常（" + code + "）";
        });
    }

    public Manifest fetch() throws Exception {
        var response = http.send(request("/api/v1/latest"), HttpResponse.BodyHandlers.ofInputStream());
        try (var in = response.body()) {
            if (response.statusCode() != 200) throw httpError(response.statusCode());
            byte[] bytes = in.readNBytes(4 * 1024 * 1024 + 1);
            if (bytes.length > 4 * 1024 * 1024) throw new IOException("清单数据过大");
            Manifest m = JSON.fromJson(new String(bytes, StandardCharsets.UTF_8), Manifest.class);
            validate(m);
            return m;
        }
    }

    void validate(Manifest m) throws IOException {
        if (m == null || m.schema_version() != 1 || !settings.packId().equals(m.pack_id())
                || m.release_id() == null || m.files() == null || m.files().isEmpty() || m.files().size() > 3000) {
            throw new IOException("清单无效、整合包标识不匹配或列表为空；已中止操作，未改动本地文件");
        }
        var seen = new HashSet<String>();
        for (Entry e : m.files()) {
            if (e == null || e.path() == null || !e.path().startsWith("mods/")) {
                throw new IOException("清单包含非法文件路径");
            }
            String name = e.path().substring(5);
            safeTarget(name);
            if (protectedName(name)) {
                throw new IOException("发布清单包含更新器自身，请服主从发布目录移除更新器");
            }
            if (!seen.add(name.toLowerCase(Locale.ROOT))) {
                throw new IOException("清单中存在重名文件: " + name);
            }
            if (e.size() < 0 || e.size() > 1024L * 1024 * 1024 || e.sha256() == null || !e.sha256().matches("[a-fA-F0-9]{64}")
                    || e.download_path() == null || !e.download_path().matches("/api/v1/files/[a-zA-Z0-9_-]+")) {
                throw new IOException("文件校验信息或下载接口格式异常");
            }
        }
    }

    /** 保护更新器本体，同时兼容识别旧版 lee-updater 与当前 modsync */
    static boolean protectedName(String name) {
        String lower = name.toLowerCase(Locale.ROOT);
        return lower.startsWith("modsync") || lower.startsWith("lee-updater");
    }

    Path safeTarget(String name) throws IOException {
        if (name == null || name.isBlank() || name.contains("/") || name.contains("\\") || name.contains(":")
                || name.matches(".*[<>\"|?*\\p{Cntrl}].*") || !name.toLowerCase(Locale.ROOT).endsWith(".jar")
                || name.matches("(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])\\..*")) {
            throw new IOException("非法 Mod 文件名: " + name);
        }
        Path p = mods.resolve(name);
        if (Files.isSymbolicLink(p) || (Files.exists(p, NOFOLLOW_LINKS) && !Files.isRegularFile(p, NOFOLLOW_LINKS))) {
            throw new IOException("拒绝操作符号链接或非普通文件：" + name);
        }
        return p;
    }

    Map<String, String> loadManaged() throws IOException {
        Path p = state.resolve("managed.json");
        if (!Files.exists(p, NOFOLLOW_LINKS)) return new LinkedHashMap<>();
        if (Files.isSymbolicLink(p)) throw new IOException("受管记录是符号链接");
        try {
            JsonObject obj = JsonParser.parseString(Files.readString(p)).getAsJsonObject();
            if (!settings.packId().equals(obj.get("packId").getAsString())) {
                throw new IOException("本地受管记录属于其他整合包标识");
            }
            Map<String, String> result = new LinkedHashMap<>();
            for (var item : obj.getAsJsonObject("files").entrySet()) {
                safeTarget(item.getKey());
                if (!protectedName(item.getKey())) {
                    result.put(item.getKey(), item.getValue().getAsString());
                }
            }
            return result;
        } catch (IOException e) {
            throw e;
        } catch (Exception e) {
            throw new IOException("受管记录损坏，停止操作", e);
        }
    }

    private void saveManaged(Map<String, String> map) throws IOException {
        Path tmp = Files.createTempFile(state, "managed-", ".tmp");
        try {
            JsonObject o = new JsonObject();
            o.addProperty("packId", settings.packId());
            o.add("files", JSON.toJsonTree(map));
            Files.writeString(tmp, JSON.toJson(o));
            try {
                Files.move(tmp, state.resolve("managed.json"), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (AtomicMoveNotSupportedException ex) {
                Files.move(tmp, state.resolve("managed.json"), StandardCopyOption.REPLACE_EXISTING);
            }
        } finally {
            Files.deleteIfExists(tmp);
        }
    }

    public Plan scan(Manifest m) throws Exception {
        validate(m);
        var managed = loadManaged();
        var missing = new ArrayList<Entry>();
        var changed = new ArrayList<Entry>();
        var names = new HashSet<String>();
        int matching = 0;

        for (Entry e : m.files()) {
            String name = e.path().substring(5);
            names.add(name.toLowerCase(Locale.ROOT));
            Path p = safeTarget(name);
            if (!Files.exists(p)) {
                missing.add(e);
            } else if (!sha256(p).equalsIgnoreCase(e.sha256())) {
                changed.add(e);
            } else {
                matching++;
            }
        }

        var removed = new ArrayList<String>();
        var extras = new ArrayList<String>();
        try (var stream = Files.list(mods)) {
            for (Path p : stream.toList()) {
                String name = p.getFileName().toString();
                if (!name.toLowerCase(Locale.ROOT).endsWith(".jar") || protectedName(name) || names.contains(name.toLowerCase(Locale.ROOT))) {
                    continue;
                }
                safeTarget(name);
                if (managed.containsKey(name)) {
                    removed.add(name);
                } else {
                    extras.add(name);
                }
            }
        }
        removed.sort(String::compareTo);
        extras.sort(String::compareTo);
        return new Plan(m, missing, changed, removed, extras, managed, matching);
    }

    public Result apply(Plan preview, boolean overwrite, boolean repair, BooleanSupplier cancelled, Consumer<String> progress) throws Exception {
        Path lock = state.resolve("update.lock");
        if (Files.isSymbolicLink(lock)) throw new IOException("非法锁文件");

        try (var channel = java.nio.channels.FileChannel.open(lock, StandardOpenOption.CREATE, StandardOpenOption.WRITE);
             var held = channel.tryLock()) {
            if (held == null) throw new IOException("另一个更新任务正在运行中");

            Manifest latest = fetch();
            if (!JSON.toJson(latest).equals(JSON.toJson(preview.manifest()))) {
                throw new IOException("服务器发布版本在此期间已发生变动，请重新检查");
            }

            Plan p = scan(latest);
            var managed = new LinkedHashMap<>(p.managed());
            int downloaded = 0, deleted = 0, skipped = 0;
            var failures = new ArrayList<String>();

            for (Entry e : latest.files()) {
                if (cancelled.getAsBoolean()) {
                    failures.add("操作已被玩家取消，未完成的项目保持原状");
                    break;
                }
                String name = e.path().substring(5);
                Path target = safeTarget(name);
                if (Files.exists(target) && sha256(target).equalsIgnoreCase(e.sha256())) {
                    managed.put(name, e.sha256());
                    continue;
                }
                if (Files.exists(target) && !overwrite) {
                    skipped++;
                    continue;
                }

                progress.accept("下载: " + name);
                try {
                    download(e, target, cancelled);
                    managed.put(name, e.sha256());
                    downloaded++;
                    saveManaged(managed);
                } catch (Exception ex) {
                    failures.add(name + "：" + friendly(ex));
                    if (cancelled.getAsBoolean()) break;
                }
            }

            // 仅在全部下载成功且未跳过、未取消时执行旧 Mod 清理，防止网络中断时误删可用文件
            if (failures.isEmpty() && skipped == 0 && !cancelled.getAsBoolean()) {
                var deletions = new ArrayList<>(p.removed());
                if (repair) {
                    deletions.addAll(p.extras());
                }
                for (String name : deletions) {
                    if (cancelled.getAsBoolean()) {
                        failures.add("已取消后续清理");
                        break;
                    }
                    try {
                        Path target = safeTarget(name);
                        if (!repair && Files.exists(target) && !sha256(target).equalsIgnoreCase(managed.get(name))) {
                            failures.add(name + "：旧受管文件被手动修改，已保留（可使用【修复】功能处理）");
                            continue;
                        }
                        progress.accept("清理: " + name);
                        if (Files.deleteIfExists(target)) {
                            deleted++;
                        }
                        managed.remove(name);
                        saveManaged(managed);
                    } catch (Exception ex) {
                        failures.add(name + "：" + friendly(ex));
                    }
                }
            }

            saveManaged(managed);
            return new Result(downloaded, deleted, skipped, List.copyOf(failures));
        }
    }

    private void download(Entry e, Path target, BooleanSupplier cancelled) throws Exception {
        int attempts = 0;
        while (true) {
            attempts++;
            Path tmp = Files.createTempFile(state, "download-", ".part");
            try {
                var r = http.send(request(e.download_path()), HttpResponse.BodyHandlers.ofInputStream());
                if (r.statusCode() == 429 && attempts < 4) {
                    try { Thread.sleep(400L * attempts); } catch (InterruptedException ignored) {}
                    continue;
                }
                if (r.statusCode() != 200) throw httpError(r.statusCode());
                try (var in = r.body(); var out = Files.newOutputStream(tmp)) {
                    byte[] buf = new byte[65536];
                    long size = 0;
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        if (cancelled.getAsBoolean()) throw new IOException("玩家已取消下载");
                        size += n;
                        if (size > e.size()) throw new IOException("下载文件大小超出发布清单");
                        out.write(buf, 0, n);
                    }
                    if (size != e.size()) throw new IOException("下载文件大小不完整");
                }

                if (!sha256(tmp).equalsIgnoreCase(e.sha256())) {
                    throw new IOException("SHA-256 完整性校验失败");
                }
                if (cancelled.getAsBoolean()) {
                    throw new IOException("玩家已取消下载");
                }

                safeTarget(target.getFileName().toString());
                try {
                    Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
                } catch (AtomicMoveNotSupportedException ex) {
                    Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
                }
                break;
            } finally {
                Files.deleteIfExists(tmp);
            }
        }
    }

    static String sha256(Path file) throws Exception {
        var md = MessageDigest.getInstance("SHA-256");
        try (var in = Files.newInputStream(file)) {
            byte[] b = new byte[65536];
            int n;
            while ((n = in.read(b)) != -1) {
                md.update(b, 0, n);
            }
        }
        return HexFormat.of().formatHex(md.digest());
    }

    public static String friendly(Throwable e) {
        if (e instanceof AccessDeniedException || e instanceof FileSystemException) {
            return "文件被占用或无写入权限，未完成操作: " + e.getMessage();
        }
        if (e instanceof java.net.ConnectException || e instanceof java.net.http.HttpTimeoutException) {
            return "无法连接更新服务器，请检查局域网连接";
        }
        return e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
    }
}
