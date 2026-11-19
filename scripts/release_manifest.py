"""Produce and verify SHA256SUMS.txt plus a machine-readable GitHub Release manifest.

Manifest describes the exact archives that have passed native smoke tests.
No checksums or sizes are taken from GitHub Actions artifact metadata.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ARCHIVE_NAME = re.compile(
    r"^anime-db-bot-(v[0-9]+\.[0-9]+\.[0-9]+)-"
    r"(windows|linux|macos)-(x64|arm64)\.(zip|tar\.gz)$"
)
EXPECTED_PLATFORMS = {
    ("windows", "x64"), ("linux", "x64"), ("linux", "arm64"),
    ("macos", "x64"), ("macos", "arm64"),
}
SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _archives(directory: Path, tag: str) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    platforms: set[tuple[str, str]] = set()
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.startswith("anime-db-bot-v"):
            continue
        match = ARCHIVE_NAME.fullmatch(path.name)
        if match is None or match.group(1) != tag:
            raise ValueError(f"Unexpected release archive: {path.name}")
        platform, arch, suffix = match.group(2, 3, 4)
        if platform == "windows" and suffix != "zip":
            raise ValueError("Windows archives must be ZIP")
        if platform != "windows" and suffix != "tar.gz":
            raise ValueError("Unix archives must be TAR.GZ")
        if (platform, arch) in platforms:
            raise ValueError(f"Duplicate archive platform: {platform}/{arch}")
        platforms.add((platform, arch))
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"Empty release archive: {path.name}")
        output.append({
            "name": path.name,
            "platform": platform,
            "architecture": arch,
            "format": suffix,
            "size": size,
            "sha256": _hash(path),
        })
    if platforms != EXPECTED_PLATFORMS:
        raise ValueError(
            "Expected 5 release platforms; missing="
            + repr(sorted(EXPECTED_PLATFORMS - platforms))
            + " unexpected=" + repr(sorted(platforms - EXPECTED_PLATFORMS))
        )
    return output


def create_release_metadata(
    directory: Path, *, tag: str, repository: str, commit: str
) -> dict[str, object]:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Expected stable semantic release tag vMAJOR.MINOR.PATCH")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
        raise ValueError("Expected full 40-character Git commit SHA")

    directory.mkdir(parents=True, exist_ok=True)
    artifacts = _archives(directory, tag)
    checksums = directory / "SHA256SUMS.txt"
    checksums.write_text(
        "".join(f"{item['sha256']}  {item['name']}\n" for item in artifacts),
        encoding="utf-8",
        newline="\n",
    )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "repository": repository,
        "release_tag": tag,
        "version": tag.removeprefix("v"),
        "source_commit": commit.lower(),
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "artifacts": artifacts,
        "checksums": {
            "name": checksums.name,
            "size": checksums.stat().st_size,
            "sha256": _hash(checksums),
        },
        "container": {
            "image": f"ghcr.io/{repository.lower()}",
            "tags": [tag.removeprefix("v"), "latest"],
            "platforms": ["linux/amd64", "linux/arm64"],
            "note": "Container tags are mutable; pin a registry digest for deployments.",
        },
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    verify_release_metadata(directory)
    return manifest


def verify_release_metadata(directory: Path) -> None:
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported release manifest schema")
    artifacts = _archives(directory, manifest["release_tag"])
    if artifacts != manifest["artifacts"]:
        raise ValueError("Release manifest differs from actual archive checksums")
    checksums = directory / "SHA256SUMS.txt"
    expected_lines = "".join(
        f"{item['sha256']}  {item['name']}\n" for item in artifacts
    )
    if checksums.read_text("utf-8") != expected_lines:
        raise ValueError("SHA256SUMS.txt contents differ from release archives")
    entry = manifest["checksums"]
    if (
        entry["name"] != checksums.name
        or entry["size"] != checksums.stat().st_size
        or not SHA256.fullmatch(entry["sha256"])
        or entry["sha256"] != _hash(checksums)
    ):
        raise ValueError("Checksum file differs from the release manifest")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("release"))
    parser.add_argument("--tag")
    parser.add_argument("--repository")
    parser.add_argument("--commit")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        verify_release_metadata(args.directory)
        print("Release manifest and SHA256SUMS verified.")
    else:
        if not args.tag or not args.repository or not args.commit:
            parser.error("--tag, --repository and --commit are required")
        manifest = create_release_metadata(
            args.directory,
            tag=args.tag,
            repository=args.repository,
            commit=args.commit,
        )
        print(f"Generated {len(manifest['artifacts'])} verified archives.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
