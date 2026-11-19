from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.release_manifest import (
    create_release_metadata,
    verify_release_metadata,
)

TAG = "v0.3.1"
SHA = "a" * 40
PLATFORMS = (
    ("linux", "x64", "tar.gz"),
    ("linux", "arm64", "tar.gz"),
    ("windows", "x64", "zip"),
    ("macos", "x64", "tar.gz"),
    ("macos", "arm64", "tar.gz"),
)


class ReleaseManifestTests(unittest.TestCase):
    def _build_archives(self, directory: Path) -> None:
        for platform, arch, extension in PLATFORMS:
            name = f"anime-db-bot-{TAG}-{platform}-{arch}.{extension}"
            (directory / name).write_bytes(f"binary-{platform}-{arch}".encode())

    def test_generated_manifest_matches_exact_files(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            self._build_archives(directory)
            result = create_release_metadata(
                directory,
                tag=TAG,
                repository="anime-db-project/anime-db-bot",
                commit=SHA,
            )
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["source_commit"], SHA)
            self.assertEqual(len(result["artifacts"]), 5)
            self.assertEqual(
                {item["name"] for item in result["artifacts"]},
                {p.name for p in directory.iterdir() if p.name.startswith("anime-db-bot-v")},
            )
            self.assertEqual(
                {item["sha256"] for item in result["artifacts"]},
                {
                    hashlib.sha256(f"binary-{platform}-{arch}".encode()).hexdigest()
                    for platform, arch, _ in PLATFORMS
                },
            )
            checksum_lines = (directory / "SHA256SUMS.txt").read_text().splitlines()
            self.assertEqual(len(checksum_lines), 5)
            manifest = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(manifest, result)
            verify_release_metadata(directory)

    def test_tampered_archive_fails_verification(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            self._build_archives(directory)
            create_release_metadata(
                directory, tag=TAG,
                repository="anime-db-project/anime-db-bot", commit=SHA,
            )
            artifact = directory / "anime-db-bot-v0.3.1-linux-x64.tar.gz"
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "manifest"):
                verify_release_metadata(directory)

    def test_tampered_checksum_file_fails(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            self._build_archives(directory)
            create_release_metadata(
                directory, tag=TAG,
                repository="anime-db-project/anime-db-bot", commit=SHA,
            )
            (directory / "SHA256SUMS.txt").write_text("bad")
            with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
                verify_release_metadata(directory)

    def test_missing_platform_blocks_release(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            self._build_archives(directory)
            (directory / "anime-db-bot-v0.3.1-macos-arm64.tar.gz").unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                create_release_metadata(
                    directory, tag=TAG,
                    repository="anime-db-project/anime-db-bot", commit=SHA,
                )

    def test_invalid_version_and_commit_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            self._build_archives(directory)
            for tag, commit in (("latest", SHA), (TAG, "shortsha")):
                with self.subTest(tag=tag, commit=commit):
                    with self.assertRaises(ValueError):
                        create_release_metadata(
                            directory,
                            tag=tag,
                            repository="anime-db-project/anime-db-bot",
                            commit=commit,
                        )


if __name__ == "__main__":
    unittest.main()
