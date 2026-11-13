import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "lineage", Path(__file__).resolve().parents[1] / "scripts/lineage.py"
)
lineage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lineage)


class LineageTests(unittest.TestCase):
    def test_failed_series_does_not_modify_source(self):
        first = b"""diff --git a/header.h b/header.h
--- a/header.h
+++ b/header.h
@@ -1 +1 @@
-before
+after
"""
        second = first.replace(b"before", b"missing").replace(b"after", b"wrong")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "header.h").write_text("before\n")
            with self.assertRaisesRegex(ValueError, "does not apply completely"):
                lineage.compose_patch(source, [("first", first), ("second", second)])
            self.assertEqual((source / "header.h").read_text(), "before\n")
            self.assertFalse((source / ".git").exists())

    def test_successful_series_handles_a_new_file_then_modifies_it(self):
        first = b"""diff --git a/new.h b/new.h
new file mode 100644
--- /dev/null
+++ b/new.h
@@ -0,0 +1 @@
+first
"""
        second = b"""diff --git a/new.h b/new.h
--- a/new.h
+++ b/new.h
@@ -1 +1 @@
-first
+second
"""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            patch, paths = lineage.compose_patch(source, [("first", first), ("second", second)])
            self.assertEqual(paths, ["new.h"])
            self.assertIn(b"+second", patch)
            self.assertNotIn(b"+first", patch)
            self.assertFalse((source / "new.h").exists())

    def test_off_cannot_silently_include_container_features(self):
        self.assertEqual(lineage.selected_configs("off", False, False), [])
        with self.assertRaises(ValueError):
            lineage.selected_configs("off", True, False)
        with self.assertRaises(ValueError):
            lineage.selected_configs("off", False, True)

    def test_mondrian_rejects_other_padding_slots(self):
        for slot in ("123", "345"):
            with self.assertRaisesRegex(ValueError, "678 or off only"):
                lineage.selected_configs(slot, False, False)

    def test_container_profile_does_not_enable_unrelated_or_abi_breaking_options(self):
        configs = lineage.selected_configs("678", True, True)
        self.assertIn("CONFIG_NTSYNC=y", configs)
        self.assertIn("CONFIG_SYSVIPC=y", configs)
        for name in ("KSU", "KSU_SUSFS", "CFS_BANDWIDTH", "CGROUP_PIDS", "CFI_PERMISSIVE"):
            self.assertNotIn(f"CONFIG_{name}=y", configs)

    def test_full_feature_profile_preserves_stock_zram_module_layout(self):
        configs = lineage.selected_configs("678", True, True, True, True)
        for name in ("NTSYNC", "TCP_CONG_BBR3", "NET_SCH_CAKE", "CIFS", "WIREGUARD",
                     "CRYPTO_LZ4K", "CRYPTO_LZ4KD", "CRYPTO_LZ4K_OPLUS"):
            self.assertIn(f"CONFIG_{name}=y", configs)
        self.assertIn("CONFIG_ZRAM=m", configs)
        self.assertIn("CONFIG_ZSMALLOC=m", configs)
        self.assertNotIn("CONFIG_ZRAM=y", configs)
        self.assertNotIn("CONFIG_ZSMALLOC=y", configs)
        with self.assertRaises(ValueError):
            lineage.selected_configs("off", False, False, True)

    def test_zram_patch_filter_excludes_module_loader_and_driver_changes(self):
        patch = b"""diff --git a/crypto/Makefile b/crypto/Makefile
--- a/crypto/Makefile
+++ b/crypto/Makefile
@@ -1 +1,2 @@
 existing
+algorithm
diff --git a/kernel/module.c b/kernel/module.c
--- a/kernel/module.c
+++ b/kernel/module.c
@@ -1 +1 @@
-check versions
+ignore versions
"""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "crypto").mkdir()
            (source / "kernel").mkdir()
            (source / "crypto/Makefile").write_text("existing\n")
            (source / "kernel/module.c").write_text("check versions\n")
            combined, paths = lineage.compose_patch(
                source, [("lz4kd.patch", patch)],
                overlays={Path("crypto/new.c"): b"new algorithm\n"},
                includes=lineage.ZRAM_PATCH_INCLUDES,
            )
            self.assertEqual(paths, ["crypto/Makefile", "crypto/new.c"])
            self.assertNotIn(b"kernel/module.c", combined)
            self.assertIn(b"new algorithm", combined)
            self.assertEqual((source / "kernel/module.c").read_text(), "check versions\n")
            self.assertFalse((source / "crypto/new.c").exists())

    def test_bbr_context_port_does_not_change_upstream_edits(self):
        patch = (b"@@ -1,4 +1,5 @@\n one_day_secs\n+upstream code\n"
                 b" \n static int tcp_min_rcvbuf = 4096;\n \n")
        adapted = lineage.port_bbr_context(patch)
        self.assertEqual([line for line in patch.splitlines() if line.startswith((b"+", b"-"))],
                         [line for line in adapted.splitlines() if line.startswith((b"+", b"-"))])
        self.assertIn(b"sysctl_tcp_low_latency", adapted)
        with self.assertRaises(ValueError):
            lineage.port_bbr_context(b"unrecognized patch")

    def test_final_config_must_retain_selected_features(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            config.write_text("CONFIG_SYSVIPC=y\n# CONFIG_PID_NS is not set\n")
            with self.assertRaisesRegex(ValueError, "CONFIG_PID_NS"):
                lineage.verify_config(config, ["CONFIG_SYSVIPC=y", "CONFIG_PID_NS=y"])
            config.write_text("CONFIG_KSU_SUSFS_SUS_PATH=y\n")
            with self.assertRaisesRegex(ValueError, "SUSFS"):
                lineage.verify_config(config, [])

    def test_crc_comparison_distinguishes_additions_changes_and_removals(self):
        with tempfile.TemporaryDirectory() as temporary:
            old, new = Path(temporary) / "old", Path(temporary) / "new"
            old.write_text("0x1234 kept vmlinux EXPORT_SYMBOL\n")
            new.write_text("0x1234 kept vmlinux EXPORT_SYMBOL\n0x5678 added vmlinux EXPORT_SYMBOL\n")
            self.assertEqual(lineage.compare_abi(old, new)["status"], "unchanged")
            new.write_text("0xffff kept vmlinux EXPORT_SYMBOL\n")
            self.assertEqual(lineage.compare_abi(old, new)["changed"], ["kept"])
            new.write_text("0x5678 added vmlinux EXPORT_SYMBOL\n")
            self.assertEqual(lineage.compare_abi(old, new)["removed"], ["kept"])

    def test_config_comparison_treats_missing_and_disabled_as_equivalent(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            config.write_text('# CONFIG_DISABLED is not set\nCONFIG_TEXT="a b"\nCONFIG_ENABLED=y\n')
            values = lineage.read_config(config)
            self.assertEqual(values["CONFIG_DISABLED"], "n")
            self.assertEqual(values["CONFIG_TEXT"], '"a b"')
            self.assertEqual(lineage.config_diff({"CONFIG_DISABLED": "n"}, {}), {})

    def test_retained_module_check_requires_matching_core_exports(self):
        reference = {
            "kernel_required_crcs": {"core": "0x1234", "module_layout": "0x4567"},
            "unique_modules": 3, "versioned_modules": 2,
            "modules_without_version_records": ["kernelsu.ko"], "scope": "test fixture",
        }
        with tempfile.TemporaryDirectory() as temporary:
            symvers = Path(temporary) / "Module.symvers"
            symvers.write_text(
                "0x1234 core vmlinux EXPORT_SYMBOL\n"
                "0x4567 module_layout vmlinux EXPORT_SYMBOL\n"
            )
            report = lineage.check_retained_modules(reference, symvers)
            self.assertEqual(report["status"], "matched")
            self.assertEqual(report["versioned_modules"], 2)
            self.assertEqual(report["modules_without_version_records"], ["kernelsu.ko"])
            symvers.write_text("0xffff core vmlinux EXPORT_SYMBOL\n")
            report = lineage.check_retained_modules(reference, symvers)
            self.assertEqual(report["status"], "mismatch")
            self.assertEqual(report["missing"], ["module_layout"])
            self.assertIn("core", report["mismatched"])

    def test_temporary_changes_restore_our_files_but_preserve_concurrent_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / "first", Path(temporary) / "second"
            first.write_text("original\n")
            changes = lineage.TemporaryChanges()
            changes.remember(first)
            changes.remember(second)
            first.write_text("generated\n")
            second.write_text("generated\n")
            changes.capture()
            first.write_text("user edit\n")
            with self.assertRaisesRegex(RuntimeError, "retained"):
                changes.restore()
            self.assertEqual(first.read_text(), "user edit\n")
            self.assertFalse(second.exists())

    def test_checksum_mismatch_is_rejected_before_building(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            profile = lineage.load_json(lineage.PROFILE)
            manifest = {
                key: profile[key]
                for key in ("device", "kernel_repository", "kernel_source", "board_config", "kernel_ref")
            }
            manifest.update({
                "schema": 1,
                "files": {"kernel.patch": "invalid", "droidspaces.config": "invalid"},
            })
            lineage.write_json(bundle / "manifest.json", manifest)
            (bundle / "kernel.patch").write_text("")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                lineage.checked_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
