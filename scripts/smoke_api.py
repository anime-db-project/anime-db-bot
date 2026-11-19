"""Native release smoke test: start the actual executable's HTTP server.

This test intentionally uses a temporary writable home and random loopback
port; it never needs a Telegram token or production databases.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _request(
    url: str, *, method: str = "GET", data: dict | None = None,
    key: str | None = None,
) -> tuple[int, dict]:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=3) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def smoke(binary: Path) -> None:
    binary = binary.resolve()
    if not binary.is_file():
        raise FileNotFoundError(binary)
    read_key = "r" * 32
    admin_key = "a" * 32
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="anime-db-bot-api-smoke-") as home:
        env = os.environ.copy()
        env.update({
            "API_READ_KEY": read_key,
            "API_ADMIN_KEY": admin_key,
            "API_AUTH_MODE": "apikey",
            "API_ENABLED": "true",
            "MAPPINGS_AUTO_UPDATE": "false",
        })
        proc = subprocess.Popen(
            [
                str(binary), "--home", home, "api",
                "--api-host", "127.0.0.1", "--api-port", str(port),
            ],
            cwd=home,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        "Native API process exited early with "
                        f"{proc.returncode}: {(proc.stdout.read() or b'')[-3000:]}"
                    )
                try:
                    status, result = _request(base + "/api/v1/health")
                    if status == 200 and result["data"]["status"] == "ok":
                        break
                except (OSError, URLError, HTTPError):
                    time.sleep(0.2)
            else:
                raise RuntimeError("Native API server never became ready")

            status, spec = _request(
                base + "/api/v1/openapi.json", key=read_key
            )
            assert status == 200 and spec["openapi"] == "3.1.0"
            assert "/graphql" in spec["paths"]
            assert "/api/v1/raw/wire" in spec["paths"]
            assert "/api/v1/raw/search" in spec["paths"]
            status, raw_id = _request(
                base + "/api/v1/raw/id/mal/5114", key=read_key
            )
            assert status == 200
            assert raw_id["data"]["sources"]["mal"]["data"]["source_id"] == "5114"
            assert raw_id["data"]["aggregated"] is False
            status, payload = _request(
                base + "/graphql", key=read_key, method="POST",
                data={"query": "{ apiVersion ping }"},
            )
            assert status == 200
            assert payload["data"]["apiVersion"] == "v1", payload
            status, raw_graphql = _request(
                base + "/graphql", key=read_key, method="POST",
                data={"query": '{ sourceCatalog rawId(source:"mal", id:"5114") }'},
            )
            assert status == 200
            assert raw_graphql["data"]["rawId"]["aggregated"] is False
            assert "wire" in raw_graphql["data"]["sourceCatalog"]["capabilities"]
            status, payload = _request(
                base + "/api/v1/operations", key=read_key
            )
            assert status == 200
            assert "search" in payload["data"]
            assert "ban" not in payload["data"]
            try:
                _request(base + "/api/v1/users", key=read_key)
            except HTTPError as exc:
                assert exc.code == 403
            else:
                raise AssertionError("A read token obtained admin-only user data")
            print(f"Native REST/GraphQL smoke passed ({binary.name})", flush=True)
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=7)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=7)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    smoke(args.binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
