package com.modsync.updater;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;

public class UpdaterCoreTest {
    static int assertions;
    static void check(boolean condition, String label) {
        if (!condition) throw new AssertionError(label);
        assertions++;
        System.out.println("PASS " + label);
    }

    static String hash(byte[] b) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(b));
    }

    static UpdaterCore.Entry entry(String name, byte[] bytes) throws Exception {
        String hash = hash(bytes);
        return new UpdaterCore.Entry(hash.substring(0, 16), "mods/" + name, bytes.length, hash, "/api/v1/files/" + hash.substring(0, 16));
    }

    static UpdaterCore.Manifest manifest(String id, UpdaterCore.Entry... entries) {
        return new UpdaterCore.Manifest(1, "test-pack", id, "测试公告", List.of(entries));
    }

    public static void main(String[] args) throws Exception {
        Path base = Files.createTempDirectory(Path.of("build"), "core-test-");
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        final UpdaterCore.Manifest[] current = new UpdaterCore.Manifest[1];
        Map<String, byte[]> bodies = new HashMap<>();
        final int[] status = {200};

        server.createContext("/", ex -> {
            int code = status[0];
            byte[] bytes = "{}".getBytes(StandardCharsets.UTF_8);
            if (!"Bearer test-token".equals(ex.getRequestHeaders().getFirst("Authorization"))) {
                code = 403;
            } else if (code == 200) {
                if (ex.getRequestURI().getPath().equals("/api/v1/latest")) {
                    bytes = UpdaterCore.JSON.toJson(current[0]).getBytes(StandardCharsets.UTF_8);
                } else {
                    bytes = bodies.get(ex.getRequestURI().getPath());
                    if (bytes == null) {
                        code = 404;
                        bytes = new byte[0];
                    }
                }
            }
            ex.sendResponseHeaders(code, bytes.length);
            try (var out = ex.getResponseBody()) {
                out.write(bytes);
            }
        });
        server.start();

        try {
            var core = new UpdaterCore(base, new UpdaterCore.Settings("http://127.0.0.1:" + server.getAddress().getPort(), "test-pack", "test-token"));

            byte[] a = "first-content".getBytes();
            byte[] b = "second-content".getBytes();
            var one = entry("a.jar", a);
            var two = entry("b.jar", b);
            bodies.put(one.download_path(), a);
            bodies.put(two.download_path(), b);
            current[0] = manifest("1", one, two);

            Files.writeString(base.resolve("mods/player.jar"), "custom user mod");

            var plan = core.scan(core.fetch());
            check(plan.missing().size() == 2, "two missing files detected");

            var result = core.apply(plan, true, false, () -> false, s -> {});
            check(result.downloaded() == 2 && result.failures().isEmpty(), "missing files downloaded and verified");
            check(Files.readString(base.resolve("mods/player.jar")).equals("custom user mod"), "extra user mod preserved");
            check(core.loadManaged().size() == 2, "managed state persisted");

            // Same length but different content detected by SHA-256
            byte[] changed = "FIRST-content".getBytes();
            var newer = entry("a.jar", changed);
            bodies.put(newer.download_path(), changed);
            current[0] = manifest("2", newer, two);
            plan = core.scan(core.fetch());
            check(plan.changed().size() == 1, "same-size content change detected");

            result = core.apply(plan, false, false, () -> false, s -> {});
            check(result.skipped() == 1 && Files.readString(base.resolve("mods/a.jar")).equals("first-content"), "skip preserves original bytes");

            result = core.apply(core.scan(core.fetch()), true, false, () -> false, s -> {});
            check(result.downloaded() == 1 && Files.readString(base.resolve("mods/a.jar")).equals("FIRST-content"), "overwrite replaces verified file");

            current[0] = manifest("3", two);
            result = core.apply(core.scan(core.fetch()), true, false, () -> false, s -> {});
            check(result.deleted() == 1 && !Files.exists(base.resolve("mods/a.jar")), "removed managed mod deleted");

            // Verify protected updater names: both modsync and legacy lee-updater are protected
            Files.writeString(base.resolve("mods/modsync-test.jar"), "protected-modsync");
            Files.writeString(base.resolve("mods/lee-updater-old.jar"), "protected-legacy");
            result = core.apply(core.scan(core.fetch()), true, true, () -> false, s -> {});
            check(result.deleted() == 1 && !Files.exists(base.resolve("mods/player.jar")), "repair removes extra mod without backup");
            check(Files.exists(base.resolve("mods/modsync-test.jar")), "repair protects modsync updater");
            check(Files.exists(base.resolve("mods/lee-updater-old.jar")), "repair protects legacy lee-updater");

            // Corrupt replacement preserves old managed mod
            current[0] = manifest("4", one);
            bodies.put(one.download_path(), "corrupt".getBytes());
            result = core.apply(core.scan(core.fetch()), true, false, () -> false, s -> {});
            check(!result.failures().isEmpty() && Files.exists(base.resolve("mods/b.jar")) && !Files.exists(base.resolve("mods/a.jar")),
                    "bad download blocks old-mod deletion");

            // Outdated publication detected
            current[0] = manifest("5", two);
            plan = core.scan(core.fetch());
            current[0] = manifest("6", two);
            boolean rejected = false;
            try {
                core.apply(plan, true, false, () -> false, s -> {});
            } catch (Exception ex) {
                rejected = true;
            }
            check(rejected, "changed publication rejected");

            // Path traversal rejection
            rejected = false;
            try {
                core.validate(manifest("7", new UpdaterCore.Entry("x", "mods/../escape.jar", 1, "0".repeat(64), "/api/v1/files/x")));
            } catch (Exception ex) {
                rejected = true;
            }
            check(rejected, "path traversal rejected");

            // Empty manifest rejection
            rejected = false;
            try {
                core.validate(manifest("8"));
            } catch (Exception ex) {
                rejected = true;
            }
            check(rejected, "empty manifest rejected");

            // Cross-origin URL rejection
            rejected = false;
            try {
                core.validate(manifest("9", new UpdaterCore.Entry("x", "mods/ok.jar", 1, "0".repeat(64), "https://other.invalid/x")));
            } catch (Exception ex) {
                rejected = true;
            }
            check(rejected, "cross-origin download rejected");

            // 503 service disabled handling
            status[0] = 503;
            rejected = false;
            try {
                core.fetch();
            } catch (Exception ex) {
                rejected = ex.getMessage().contains("503");
            }
            check(rejected, "disabled server handled");

            // Bad token rejection
            status[0] = 200;
            var wrong = new UpdaterCore(base.resolve("wrong"), new UpdaterCore.Settings("http://127.0.0.1:" + server.getAddress().getPort(), "test-pack", "wrong"));
            rejected = false;
            try {
                wrong.fetch();
            } catch (Exception ex) {
                rejected = ex.getMessage().contains("403");
            }
            check(rejected, "bad token rejected");

            // Cancellation test
            current[0] = manifest("10", one);
            bodies.put(one.download_path(), a);
            result = core.apply(core.scan(core.fetch()), true, false, () -> true, s -> {});
            check(!Files.exists(base.resolve("mods/a.jar")) && Files.exists(base.resolve("mods/b.jar")), "cancel leaves files untouched");
            check(Files.list(base.resolve(".modsync")).noneMatch(p -> p.toString().endsWith(".part")), "temporary downloads cleaned");

            // Legacy state migration test
            Path legacyInstance = Files.createTempDirectory(Path.of("build"), "legacy-test-");
            Path legState = legacyInstance.resolve(".lee-updater");
            Files.createDirectories(legState);
            Files.writeString(legState.resolve("managed.json"), "{\"packId\":\"test-pack\",\"files\":{\"legacy.jar\":\"" + hash(a) + "\"}}");
            var migratedCore = new UpdaterCore(legacyInstance, new UpdaterCore.Settings("http://127.0.0.1:" + server.getAddress().getPort(), "test-pack", "test-token"));
            check(migratedCore.loadManaged().containsKey("legacy.jar"), "legacy managed.json migrated from .lee-updater to .modsync");

            System.out.println("SUCCESS " + assertions + " assertions; fixtures: " + base);
        } finally {
            server.stop(0);
        }
    }
}
