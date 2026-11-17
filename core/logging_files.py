from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

_LOG_STREAMS = frozenset({"bot", "users", "api"})


def classify_log_stream(record: logging.LogRecord) -> str:
    """Route a record to one stable low-cardinality file stream."""
    explicit = str(getattr(record, "log_stream", "") or "").strip().casefold()
    if explicit in _LOG_STREAMS:
        return explicit

    event = str(getattr(record, "event", "") or "").strip().casefold()
    if event == "telegram_update" or event.startswith("user_"):
        return "users"
    if event.startswith("source_"):
        return "api"
    return "bot"


class DailySplitFileHandler(logging.Handler):
    """Write records to date-scoped bot/users/api files with bounded size rotation."""

    def __init__(
        self,
        directory: Path | str,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backups: int = 5,
        retention_days: int = 30,
    ) -> None:
        super().__init__()
        self.directory = Path(directory)
        self.max_bytes = max(1, int(max_bytes))
        self.backups = max(0, int(backups))
        self.retention_days = max(1, int(retention_days))
        self._handles: dict[Path, TextIO] = {}
        self._sizes: dict[Path, int] = {}
        self._open_day: str | None = None

    def _close_files(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()
        self._sizes.clear()

    def _expire_old_files(self, local_day: datetime) -> None:
        cutoff = local_day.date() - timedelta(days=self.retention_days)
        if not self.directory.is_dir():
            return
        for folder in self.directory.iterdir():
            if not folder.is_dir() or folder.is_symlink():
                continue
            if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", folder.name) is None:
                continue
            try:
                folder_date = datetime.strptime(folder.name, "%Y.%m.%d").date()
            except ValueError:
                continue
            if folder_date >= cutoff:
                continue
            for file in folder.iterdir():
                if (
                    file.is_file()
                    and not file.is_symlink()
                    and re.fullmatch(
                        r"(?:bot|users|api|errors)\.log(?:\.\d+)?", file.name
                    )
                ):
                    file.unlink()
            if not any(folder.iterdir()):
                folder.rmdir()

    def close(self) -> None:
        try:
            self._close_files()
        finally:
            super().close()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            rendered = self.format(record)
            local_time = datetime.fromtimestamp(record.created).astimezone()
            day = local_time.strftime("%Y.%m.%d")
            day_dir = self.directory / day
            if self._open_day != day:
                self._close_files()
                self._open_day = day
                day_dir.mkdir(parents=True, exist_ok=True)
                self._expire_old_files(local_time)

            stream = classify_log_stream(record)
            self._write(day_dir / f"{stream}.log", rendered)
            if record.levelno >= logging.ERROR:
                self._write(day_dir / "errors.log", rendered)
        except Exception:
            self.handleError(record)

    def _write(self, path: Path, rendered: str) -> None:
        line = f"{rendered}\n"
        encoded_size = len(line.encode("utf-8"))
        size = self._sizes.get(path)
        if size is None:
            size = path.stat().st_size if path.exists() else 0
        if size + encoded_size > self.max_bytes:
            handle = self._handles.pop(path, None)
            if handle is not None:
                handle.close()
            self._rotate_if_needed(path, encoded_size)
            size = path.stat().st_size if path.exists() else 0
        handle = self._handles.get(path)
        if handle is None:
            handle = path.open("a", encoding="utf-8")
            self._handles[path] = handle
        handle.write(line)
        handle.flush()
        self._sizes[path] = size + encoded_size

    def _rotate_if_needed(self, path: Path, incoming_bytes: int) -> None:
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= self.max_bytes:
            return

        if self.backups == 0:
            path.write_text("", encoding="utf-8")
            return

        oldest = path.with_name(f"{path.name}.{self.backups}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backups - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
        if path.exists():
            os.replace(path, path.with_name(f"{path.name}.1"))
