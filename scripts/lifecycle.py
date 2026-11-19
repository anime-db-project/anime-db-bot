"""Cross-platform supervisor for the anime-db-bot process.

Run with the active virtual environment: python -m scripts.lifecycle.
The supervisor owns the bot subprocess and is the process managed by tmux or
the cross-platform launcher. Expected operator signals always stop both.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time

from core.settings import LifecycleSettings, ROOT_DIR, get_settings

BOT_MODULE = "bots.telegram.bot"


def _info(message: str) -> None:
    print(f"[anime-db-bot lifecycle] {message}", flush=True)


def restart_delay(policy: LifecycleSettings, attempt: int) -> float:
    """Exponential delay bounded by the configured maximum."""
    if attempt < 1:
        raise ValueError("attempt must be positive")
    return min(
        policy.restart_max_delay_seconds,
        policy.restart_delay_seconds * (2 ** min(attempt - 1, 16)),
    )


def restart_allowed(
    policy: LifecycleSettings,
    returncode: int,
    attempts: int,
) -> bool:
    """Decide on a completed child exit; operator stops are handled separately."""
    action = policy.on_clean_exit if returncode == 0 else policy.on_failure
    return action == "restart" and attempts < policy.restart_max_attempts


def _stop_child(
    process: subprocess.Popen[bytes], timeout_seconds: float
) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _info("Graceful shutdown timed out; terminating the bot forcefully.")
        try:
            process.kill()
        except ProcessLookupError:
            return
        process.wait()


def supervise_bot(policy: LifecycleSettings) -> int:
    """Keep the bot running according to the configured exit policy.

    All restarts occur in this parent, so worker crashes never duplicate
    Telegram polling instances. SIGINT/SIGTERM/SIGHUP explicitly stop the
    supervisor and child regardless of the restart policy.
    """
    stopping = threading.Event()
    previous_handlers: dict[signal.Signals, object] = {}

    def request_stop(signum: int, _frame: object) -> None:
        _info(f"Received shutdown signal {signum}.")
        stopping.set()

    watched = (signal.SIGINT, signal.SIGTERM)
    if hasattr(signal, "SIGHUP"):
        watched += (signal.SIGHUP,)
    for watched_signal in watched:
        previous_handlers[watched_signal] = signal.getsignal(watched_signal)
        signal.signal(watched_signal, request_stop)

    attempts = 0
    try:
        while not stopping.is_set():
            started = time.monotonic()
            try:
                child = subprocess.Popen(
                    [sys.executable, "-m", BOT_MODULE],
                    cwd=ROOT_DIR,
                )
            except OSError as exc:
                # Do not retry broken executables or permissions indefinitely.
                _info(f"Could not start the bot: {type(exc).__name__}.")
                return 1

            _info(f"Bot process started (PID {child.pid}).")
            returncode: int | None = None
            try:
                while not stopping.is_set():
                    try:
                        returncode = child.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if stopping.is_set():
                    _stop_child(child, policy.shutdown_timeout_seconds)

            if stopping.is_set():
                _info("Bot stopped by operator request.")
                return 0

            assert returncode is not None
            uptime = time.monotonic() - started
            if uptime >= policy.restart_reset_after_seconds:
                attempts = 0

            if not restart_allowed(policy, returncode, attempts):
                action = policy.on_clean_exit if returncode == 0 else policy.on_failure
                if action == "restart" and policy.restart_max_attempts <= attempts:
                    _info("Restart attempt limit reached; stopping supervisor.")
                else:
                    _info(f"Bot exited with code {returncode}; action: stop.")
                return returncode if returncode != 0 else 0

            attempts += 1
            delay = restart_delay(policy, attempts)
            _info(
                f"Bot exited with code {returncode}; restarting "
                f"({attempts}/{policy.restart_max_attempts}) in {delay:g}s."
            )
            stopping.wait(delay)
        return 0
    finally:
        for watched_signal, previous_handler in previous_handlers.items():
            signal.signal(watched_signal, previous_handler)


def main() -> int:
    try:
        settings = get_settings()
        settings.require_bot_token()
        return supervise_bot(settings.lifecycle)
    except (ValueError, RuntimeError) as exc:
        # Do not print configuration values (they may contain credentials).
        _info(f"Supervisor configuration error: {type(exc).__name__}.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
