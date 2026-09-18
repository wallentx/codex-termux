#!/usr/bin/env python3
"""Validate the V8 bindgen preflight without running Cargo or loading LLVM."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "rusty_v8_android_release", ROOT / ".github/scripts/rusty_v8_android_release.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BindgenToolchainTests(unittest.TestCase):
    def test_supported_versions_use_selected_library_and_dispose_string(self):
        for version in (b"Ubuntu clang version 21.1.8", b"clang version 22.1.0"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
            ):
                library = Mock()
                library.clang_getCString.return_value = version
                with patch.object(MODULE.ctypes, "CDLL", return_value=library) as load:
                    MODULE.validate_android_bindgen_toolchain(
                        {"LIBCLANG_PATH": directory}
                    )
                load.assert_called_once_with(str(Path(directory) / "libclang.so"))
                library.clang_disposeString.assert_called_once_with(
                    library.clang_getClangVersion.return_value
                )

    def test_rejects_old_and_unrecognized_versions(self):
        for version in (
            b"Ubuntu clang version 19.1.7",
            b"clang version 21.0.0",
            b"unknown",
            None,
        ):
            with self.subTest(version=version):
                library = Mock()
                library.clang_getCString.return_value = version
                with (
                    patch.object(MODULE.ctypes, "CDLL", return_value=library),
                    self.assertRaisesRegex(SystemExit, r"require libclang 21\.1\+"),
                ):
                    MODULE.validate_android_bindgen_toolchain(
                        {"LIBCLANG_PATH": "/toolchain/libclang.so"}
                    )
                library.clang_disposeString.assert_called_once()

    def test_accepts_explicit_library_file(self):
        library = Mock()
        library.clang_getCString.return_value = b"clang version 21.1.0"
        with patch.object(MODULE.ctypes, "CDLL", return_value=library) as load:
            MODULE.validate_android_bindgen_toolchain(
                {"LIBCLANG_PATH": "/toolchain/libclang.so"}
            )
        load.assert_called_once_with("/toolchain/libclang.so")

    def test_requires_explicit_library_path(self):
        with self.assertRaisesRegex(SystemExit, "requires LIBCLANG_PATH"):
            MODULE.validate_android_bindgen_toolchain({})

    def test_unloadable_library_has_actionable_error(self):
        with (
            patch.object(MODULE.ctypes, "CDLL", side_effect=OSError("missing library")),
            self.assertRaisesRegex(SystemExit, "cannot load bindgen libclang"),
        ):
            MODULE.validate_android_bindgen_toolchain(
                {"LIBCLANG_PATH": "/missing/libclang.so"}
            )

    def test_non_libclang_library_is_rejected(self):
        with (
            patch.object(MODULE.ctypes, "CDLL", return_value=object()),
            self.assertRaisesRegex(SystemExit, "cannot load bindgen libclang"),
        ):
            MODULE.validate_android_bindgen_toolchain(
                {"LIBCLANG_PATH": "/wrong/library.so"}
            )

    def test_failed_preflight_runs_before_cargo_or_temp_workspace(self):
        with (
            patch.object(
                MODULE,
                "validate_android_bindgen_toolchain",
                side_effect=SystemExit("old libclang"),
            ),
            patch.object(MODULE.subprocess, "run") as run,
            patch.object(MODULE.tempfile, "mkdtemp") as create_workspace,
            self.assertRaisesRegex(SystemExit, "old libclang"),
        ):
            MODULE.stage_android_release_pair(
                Path("source"), "aarch64-linux-android", Path("dist"), Path("v8")
            )
        run.assert_not_called()
        create_workspace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
