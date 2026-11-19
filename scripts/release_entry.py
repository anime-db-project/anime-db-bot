"""Portable entry point for the source checkout, frozen binaries and Docker.

Public usage: anime-db-bot [run|start|stop|restart|status|init|check|doctor]
No subcommand runs in the foreground, including double-clicked executables.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time

from scripts.build_version import VERSION

ENV_GROUPS = (
    "telegram.env", "sources.env", "timestamps.env", "rate-limits.env",
    "mappings.env", "storage.env", "logging.env", "runtime.env",
    "lifecycle.env",
)
PROFILE_FILES = (
    "telegram.toml", "shikimori.toml", "mal.toml", "anilist.toml",
    "kitsu.toml", "anidb.toml", "anisearch.toml", "jikan.toml",
    "aniskip.toml", "anime_skip.toml", "animethemes.toml", "imdb.toml",
    "skipdb.toml", "trace_moe.toml", "whatanimeisthis.toml",
)


def info(message: str) -> None:
    print(f"[anime-db-bot] {message}", flush=True)


def error(message: str) -> int:
    print(f"[anime-db-bot] ERROR: {message}", file=sys.stderr, flush=True)
    return 2


def runtime_home(value: str | None = None) -> Path:
    home = value or os.environ.get("ANIME_DB_BOT_HOME", "")
    if home:
        return Path(home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resources_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def _pid_path(home: Path) -> Path:
    return home / "data" / "runtime" / "anime-db-bot-release.pid"


def _stop_path(home: Path) -> Path:
    return home / "data" / "runtime" / "anime-db-bot-release.stop"


def _launcher_log(home: Path) -> Path:
    return home / "logs" / "launcher.log"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return result.returncode == 0 and (
            f'","{pid}","' in result.stdout
            or f'","{pid}"' in result.stdout
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def current_pid(home: Path) -> int | None:
    file = _pid_path(home)
    try:
        value = int(json.loads(file.read_text(encoding="utf-8"))["pid"])
    except (FileNotFoundError, ValueError, KeyError, OSError, TypeError):
        return None
    if pid_alive(value):
        return value
    file.unlink(missing_ok=True)
    return None


def _register_process(home: Path) -> bool:
    file = _pid_path(home)
    file.parent.mkdir(parents=True, exist_ok=True)
    if current_pid(home) is not None:
        return False
    _stop_path(home).unlink(missing_ok=True)
    try:
        with file.open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid()}, stream)
    except FileExistsError:
        return False
    return True


def _remove_pid_if_owned(home: Path) -> None:
    file = _pid_path(home)
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
        if int(value.get("pid", 0)) == os.getpid():
            file.unlink(missing_ok=True)
            _stop_path(home).unlink(missing_ok=True)
    except (FileNotFoundError, ValueError, OSError, TypeError):
        pass


def _copy_examples(home: Path, *, force: bool = False) -> bool:
    """Create missing active configuration without overwriting user settings."""
    root = resources_root()
    if not (root / ".env.example.d").is_dir():
        raise RuntimeError("Bundled configuration templates are missing")
    if (home / ".env").is_file():
        return False
    active = home / ".env.d"
    if not force and any((active / key).exists() for key in ENV_GROUPS):
        return False
    active.mkdir(parents=True, exist_ok=True)
    created = False
    for filename in ENV_GROUPS:
        template = root / ".env.example.d" / filename
        if not template.is_file():
            raise RuntimeError(f"Missing bundled template {filename}")
        dest = active / filename
        if not dest.exists():
            shutil.copyfile(template, dest)
            if os.name != "nt":
                dest.chmod(0o600)
            created = True
    for kind in ("proxies", "user-agents"):
        templates = root / f".{kind}.example.d"
        target = home / f".{kind}.d"
        if not templates.is_dir():
            raise RuntimeError(f"Missing bundled {kind} templates")
        target.mkdir(parents=True, exist_ok=True)
        for filename in PROFILE_FILES:
            src = templates / filename
            if src.is_file() and not (target / filename).exists():
                shutil.copyfile(src, target / filename)
                if os.name != "nt":
                    (target / filename).chmod(0o600)
    return created


def _load_external_env(path: str) -> None:
    from dotenv import dotenv_values

    file = Path(path).expanduser().resolve()
    if not file.is_file():
        raise ValueError(f"Environment file does not exist: {file}")
    for key, value in dotenv_values(file).items():
        if value is not None:
            os.environ.setdefault(key, value)


def _set_cli_overrides(args: argparse.Namespace) -> None:
    values = {
        "LOG_LEVEL": args.log_level,
        "BOT_ON_FAILURE": args.on_failure,
        "BOT_ON_CLEAN_EXIT": args.on_clean_exit,
        "BOT_RESTART_MAX_ATTEMPTS": args.restart_limit,
        "BOT_RESTART_DELAY_SECONDS": args.restart_delay,
        "TELEGRAM_MAX_CONCURRENT_UPDATES": args.max_updates,
    }
    for key, value in values.items():
        if value is not None:
            os.environ[key] = str(value)
    if args.no_mappings_update:
        os.environ["MAPPINGS_AUTO_UPDATE"] = "false"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anime-db-bot",
        description="Portable anime-db-bot: interactive, background and Docker runtime.",
    )
    parser.add_argument(
        "command", nargs="?", default="run",
        choices=("run", "start", "stop", "restart", "status", "init", "check", "doctor"),
        help="Action; default: run in foreground (double-click friendly).",
    )
    parser.add_argument(
        "--home", metavar="DIRECTORY",
        help="Writable home for .env.d, data and logs; overrides ANIME_DB_BOT_HOME.",
    )
    parser.add_argument(
        "--env-file", metavar="FILE",
        help="Additional dotenv file; real environment and explicit flags win.",
    )
    parser.add_argument("--log-level", metavar="LEVEL")
    parser.add_argument("--on-failure", choices=("stop", "restart"))
    parser.add_argument("--on-clean-exit", choices=("stop", "restart"))
    parser.add_argument("--restart-limit", type=int, metavar="COUNT")
    parser.add_argument("--restart-delay", type=float, metavar="SECONDS")
    parser.add_argument("--max-updates", type=int, metavar="COUNT")
    parser.add_argument("--no-mappings-update", action="store_true")
    parser.add_argument("--version", action="version", version=f"anime-db-bot {VERSION}")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    return parser


def _run_supervisor(home: Path, settings: object, *, managed: bool) -> int:
    from scripts.lifecycle import supervise_bot

    if not _register_process(home):
        return error(f"Another instance is already running (home: {home}).")
    try:
        info(f"Starting v{VERSION} on {platform.system()} {platform.machine()}")
        info(f"Mode: {'background supervisor' if managed else 'foreground'}")
        info(f"Home: {home}")
        info(f"Logs: {settings.logging.directory}")
        info(
            f"Exit policies: failure={settings.lifecycle.on_failure}, "
            f"clean={settings.lifecycle.on_clean_exit}, "
            f"restart limit={settings.lifecycle.restart_max_attempts}"
        )
        info(
            f"Storage: {settings.storage.backend}; "
            f"search cache: {settings.cache.backend}"
        )
        return supervise_bot(settings.lifecycle, stop_file=_stop_path(home))
    finally:
        _remove_pid_if_owned(home)


def _supervisor_command(home: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--home", str(home), "--supervise"]
    return [
        sys.executable, "-m", "scripts.release_entry",
        "--home", str(home), "--supervise",
    ]


def _start_background(home: Path) -> int:
    running = current_pid(home)
    if running is not None:
        info(f"Bot is already running (supervisor PID {running}).")
        return 0

    logfile = _launcher_log(home)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    command = _supervisor_command(home)
    options: dict[str, object] = {
        "cwd": home,
        "stdin": subprocess.DEVNULL,
        "stdout": None,
        "stderr": subprocess.STDOUT,
        "env": env,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        options["start_new_session"] = True
    with logfile.open("ab", buffering=0) as output:
        options["stdout"] = output
        process = subprocess.Popen(command, **options)
    for _ in range(30):
        if current_pid(home) == process.pid:
            info(f"Background supervisor started (PID {process.pid}).")
            info(f"Output: {logfile}")
            info(f"Stop: anime-db-bot stop --home {home}")
            return 0
        if process.poll() is not None:
            return error(f"Bot exited with code {process.returncode}; see {logfile}")
        time.sleep(0.1)
    return error(f"Supervisor did not become ready; inspect {logfile}")


def _stop_background(home: Path, *, timeout: float = 15.0) -> int:
    pid = current_pid(home)
    if pid is None:
        info("No managed bot process is running.")
        return 0
    info(f"Requesting graceful stop for supervisor PID {pid}.")
    file = _stop_path(home)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(str(pid), encoding="utf-8")
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if not pid_alive(pid):
            info("Bot stopped.")
            return 0
        time.sleep(0.2)

    info("Stop timed out; forcing supervisor process tree to terminate.")
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if not pid_alive(pid):
        _pid_path(home).unlink(missing_ok=True)
        file.unlink(missing_ok=True)
        return 0
    return error("Could not confirm process termination; inspect the supervisor.")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(arguments)
    home = runtime_home(args.home)
    os.environ["ANIME_DB_BOT_HOME"] = str(home)

    try:
        if args.env_file:
            _load_external_env(args.env_file)
        # Explicit command-line switches override both imported and local env.
        _set_cli_overrides(args)
        from core.settings import (
            AppSettings,
            load_project_environment,
            project_env_values,
        )
        from core.settings import RESOURCE_DIR

        if args.command == "init" and not args.worker:
            made = _copy_examples(home, force=True)
            info(f"Config {'initialized' if made else 'already exists'}: {home}")
            return 0
        if args.command in {"run", "start", "restart"} and not args.worker:
            if _copy_examples(home):
                info(f"Created config templates in {home / '.env.d'}")
        load_project_environment()
        settings = AppSettings.from_env(os.environ)
        if args.command == "doctor":
            info(f"Version: {VERSION}")
            info(f"OS: {platform.platform()} [{platform.machine()}]")
            info(f"Frozen: {bool(getattr(sys, 'frozen', False))}")
            info(f"Home: {home}")
            info(f"Bundled resources: {RESOURCE_DIR}")
            info(f"Templates: {(resources_root() / '.env.example.d').is_dir()}")
            info(f"Data: {home / 'data'}")
            return 0
        if args.command == "check":
            project_env_values()
            info(f"Configuration is valid ({home}).")
            if not settings.telegram.bot_token:
                info("BOT_TOKEN is empty; the bot cannot be started yet.")
            return 0
        if args.command == "status":
            pid = current_pid(home)
            info(
                f"Bot {'running (supervisor PID ' + str(pid) + ')' if pid else 'not running'}."
            )
            info(f"Home: {home}")
            return 0 if pid else 1
        if args.command == "stop":
            return _stop_background(
                home, timeout=settings.lifecycle.shutdown_timeout_seconds + 5
            )
        if args.command == "restart":
            stopped = _stop_background(
                home, timeout=settings.lifecycle.shutdown_timeout_seconds + 5
            )
            if stopped:
                return stopped
        if not settings.telegram.bot_token:
            return error(
                f"BOT_TOKEN is missing. Set it in {home / '.env.d' / 'telegram.env'} "
                "or supply BOT_TOKEN as an environment variable."
            )
        if args.worker:
            from bots.telegram.bot import main as run_worker
            run_worker()
            return 0
        if args.supervise:
            return _run_supervisor(home, settings, managed=True)
        if args.command in {"start", "restart"}:
            return _start_background(home)
        return _run_supervisor(home, settings, managed=False)
    except (OSError, RuntimeError, ValueError) as exc:
        # Never render raw dotenv values or exception details containing secrets.
        return error(f"Startup/configuration error: {type(exc).__name__}. "
                     "Check .env.d and the launcher log.")


if __name__ == "__main__":
    result = main()
    if result and not sys.argv[1:] and os.name == "nt" and sys.stdin.isatty():
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(result)
