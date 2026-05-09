#!/usr/bin/env python3

from __future__ import annotations

import gzip
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import rusty_v8_bazel
import rusty_v8_module_bazel


class RustyV8BazelTest(unittest.TestCase):
    def test_update_module_bazel_replaces_and_inserts_sha256(self) -> None:
        module_bazel = textwrap.dedent(
            """\
            http_file(
                name = "rusty_v8_146_4_0_x86_64_unknown_linux_gnu_archive",
                downloaded_file_path = "librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                sha256 = "0000000000000000000000000000000000000000000000000000000000000000",
                urls = [
                    "https://example.test/librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                ],
            )

            http_file(
                name = "rusty_v8_146_4_0_x86_64_unknown_linux_musl_binding",
                downloaded_file_path = "src_binding_release_x86_64-unknown-linux-musl.rs",
                urls = [
                    "https://example.test/src_binding_release_x86_64-unknown-linux-musl.rs",
                ],
            )

            http_file(
                name = "rusty_v8_145_0_0_x86_64_unknown_linux_gnu_archive",
                downloaded_file_path = "librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                sha256 = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                urls = [
                    "https://example.test/old.gz",
                ],
            )
            """
        )
        checksums = {
            "librusty_v8_release_x86_64-unknown-linux-gnu.a.gz": (
                "1111111111111111111111111111111111111111111111111111111111111111"
            ),
            "src_binding_release_x86_64-unknown-linux-musl.rs": (
                "2222222222222222222222222222222222222222222222222222222222222222"
            ),
        }

        updated = rusty_v8_module_bazel.update_module_bazel_text(
            module_bazel,
            checksums,
            "146.4.0",
        )

        self.assertEqual(
            textwrap.dedent(
                """\
                http_file(
                    name = "rusty_v8_146_4_0_x86_64_unknown_linux_gnu_archive",
                    downloaded_file_path = "librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                    sha256 = "1111111111111111111111111111111111111111111111111111111111111111",
                    urls = [
                        "https://example.test/librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                    ],
                )

                http_file(
                    name = "rusty_v8_146_4_0_x86_64_unknown_linux_musl_binding",
                    downloaded_file_path = "src_binding_release_x86_64-unknown-linux-musl.rs",
                    sha256 = "2222222222222222222222222222222222222222222222222222222222222222",
                    urls = [
                        "https://example.test/src_binding_release_x86_64-unknown-linux-musl.rs",
                    ],
                )

                http_file(
                    name = "rusty_v8_145_0_0_x86_64_unknown_linux_gnu_archive",
                    downloaded_file_path = "librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                    sha256 = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                    urls = [
                        "https://example.test/old.gz",
                    ],
                )
                """
            ),
            updated,
        )
        rusty_v8_module_bazel.check_module_bazel_text(updated, checksums, "146.4.0")

    def test_check_module_bazel_rejects_manifest_drift(self) -> None:
        module_bazel = textwrap.dedent(
            """\
            http_file(
                name = "rusty_v8_146_4_0_x86_64_unknown_linux_gnu_archive",
                downloaded_file_path = "librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                sha256 = "1111111111111111111111111111111111111111111111111111111111111111",
                urls = [
                    "https://example.test/librusty_v8_release_x86_64-unknown-linux-gnu.a.gz",
                ],
            )
            """
        )
        checksums = {
            "librusty_v8_release_x86_64-unknown-linux-gnu.a.gz": (
                "1111111111111111111111111111111111111111111111111111111111111111"
            ),
            "orphan.gz": (
                "2222222222222222222222222222222222222222222222222222222222222222"
            ),
        }

        with self.assertRaisesRegex(
            rusty_v8_module_bazel.RustyV8ChecksumError,
            "manifest has orphan.gz",
        ):
            rusty_v8_module_bazel.check_module_bazel_text(
                module_bazel,
                checksums,
                "146.4.0",
            )

    @patch("rusty_v8_bazel.resolved_v8_crate_version", return_value="146.4.0")
    @patch("rusty_v8_bazel.subprocess.run")
    def test_stage_android_release_pair_stages_cargo_source_outputs(
        self,
        run: Mock,
        _resolved_version: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            stage_root = temp_path / "stage"
            output_dir = temp_path / "dist"
            cargo_home = temp_path / "cargo-home"
            rusty_v8_source = temp_path / "rusty-v8-source"
            crate_source = cargo_home / "registry" / "src" / "index" / "v8-146.4.0"
            source_vendor_crate = (
                rusty_v8_source
                / "third_party"
                / "rust"
                / "chromium_crates_io"
                / "vendor"
                / "icu_calendar_data-v2"
            )
            stage_root.mkdir()
            crate_source.mkdir(parents=True)
            source_vendor_crate.mkdir(parents=True)
            (source_vendor_crate / "build.rs").write_text(
                "// vendor build\n",
                encoding="utf-8",
            )

            def fake_run(*_args: object, **kwargs: object) -> Mock:
                args = _args[0]
                env = kwargs["env"]
                self.assertIsInstance(env, dict)
                if args[0] != "cargo" or args[1] == "fetch":
                    return Mock(returncode=0)

                build_dir = (
                    Path(env["CARGO_TARGET_DIR"])
                    / "aarch64-linux-android"
                    / "release"
                    / "gn_out"
                )
                (build_dir / "obj").mkdir(parents=True)
                (build_dir / "obj" / "librusty_v8.a").write_bytes(b"archive")
                (build_dir / "src_binding.rs").write_text(
                    "// binding\n",
                    encoding="utf-8",
                )
                return Mock(returncode=0)

            run.side_effect = fake_run
            with (
                patch("rusty_v8_bazel.tempfile.mkdtemp", return_value=str(stage_root)),
                patch.dict("rusty_v8_bazel.os.environ", {"CARGO_HOME": str(cargo_home)}),
            ):
                rusty_v8_bazel.stage_android_release_pair(
                    "aarch64-linux-android",
                    output_dir,
                    rusty_v8_source,
                )

            archive = output_dir / "librusty_v8_release_aarch64-linux-android.a.gz"
            binding = output_dir / "src_binding_release_aarch64-linux-android.rs"
            checksums = output_dir / "rusty_v8_release_aarch64-linux-android.sha256"
            vendored_pydeps = (
                stage_root
                / "v8-146.4.0"
                / "build"
                / "android"
                / "pylib"
                / "results"
                / "presentation"
                / "test_results_presentation.pydeps"
            )
            vendored_rust_build = (
                stage_root
                / "v8-146.4.0"
                / "third_party"
                / "rust"
                / "chromium_crates_io"
                / "vendor"
                / "icu_calendar_data-v2"
                / "build.rs"
            )
            self.assertEqual(b"archive", gzip.decompress(archive.read_bytes()))
            self.assertEqual("// binding\n", binding.read_text(encoding="utf-8"))
            self.assertTrue(vendored_pydeps.exists())
            self.assertEqual(
                "// vendor build\n",
                vendored_rust_build.read_text(encoding="utf-8"),
            )
            self.assertIn(archive.name, checksums.read_text(encoding="utf-8"))
            self.assertIn(binding.name, checksums.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
