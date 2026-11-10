"""
Shared logging setup for dream scripts.

Provides timestamped console + file logging with daily rotation, size cap,
and age-based cleanup.

Log file naming: <name>.YYYY-MM-DD.<n>.log
  e.g. dream.2026-06-03.0.log, dream.2026-06-03.1.log (if size exceeded)

Usage from any dream script:

    from dream_logging import setup_logging

    # In argparse:
    ap.add_argument("--log-file", default="logs/dream/dream.log")
    ap.add_argument("--log-max-mb", type=float, default=5.0)
    ap.add_argument("--log-keep-days", type=int, default=30)

    # After parsing:
    setup_logging(
        log_file=args.log_file,       # base name or path
        max_bytes=int(args.log_max_mb * 1024 * 1024),
        keep_days=args.log_keep_days,
    )

    # Then just use print() — stdout/stderr are redirected to the logger.
"""

import glob
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from logging.handlers import BaseRotatingHandler


LOG_FORMAT = "%(asctime)s %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def _resolve_log_path(log_file: str) -> str:
    """Keep legacy log paths in the source directory out of that directory."""
    path = os.path.abspath(log_file)
    source_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.normcase(os.path.dirname(path)) == os.path.normcase(source_dir):
        return os.path.join(os.path.dirname(source_dir), "logs", "dream",
                            os.path.basename(path))
    return path


class DreamFileHandler(BaseRotatingHandler):
    """Daily + size-based rotating handler.

    Active log:   <stem>.YYYY-MM-DD.0.log
    Rolled over:  <stem>.YYYY-MM-DD.1.log, .2.log, ...

    When the file exceeds *maxBytes*, a new sequence number is started.
    When the date changes, the sequence resets to 0.
    Old files beyond *keepDays* are deleted at startup and on each rollover.
    """

    def __init__(self, base_path: str, maxBytes: int = 5 * 1024 * 1024,
                 keepDays: int = 30, encoding: str = "utf-8"):
        self._base_dir = os.path.dirname(os.path.abspath(base_path)) or "."
        os.makedirs(self._base_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(base_path))[0]
        self._stem = stem
        self._maxBytes = maxBytes
        self._keepDays = keepDays

        self._today = datetime.now().strftime("%Y-%m-%d")
        self._seq = self._next_seq(self._today)

        filename = self._make_filename(self._today, self._seq)
        super().__init__(filename, mode="a", encoding=encoding)
        self._purge_old()

    def _make_filename(self, date_str: str, seq: int) -> str:
        return os.path.join(self._base_dir, f"{self._stem}.{date_str}.{seq}.log")

    def _glob_pattern(self) -> str:
        return os.path.join(self._base_dir, f"{self._stem}.*.*.log")

    def _next_seq(self, date_str: str) -> int:
        """Find the highest existing sequence number for a date, or 0."""
        pattern = re.compile(
            re.escape(self._stem) + r"\." + re.escape(date_str) + r"\.(\d+)\.log$"
        )
        max_seq = -1
        for path in glob.glob(self._make_filename(date_str, seq=0).replace(".0.log", ".*.log")):
            m = pattern.search(os.path.basename(path))
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        return max(0, max_seq)

    def shouldRollover(self, record) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today:
            return 1
        if self._maxBytes > 0 and self.stream is not None:
            self.stream.seek(0, 2)
            if self.stream.tell() >= self._maxBytes:
                return 1
        return 0

    def doRollover(self):
        if self.stream:
            self.stream.close()

        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._seq = 0
        else:
            self._seq += 1

        self.baseFilename = self._make_filename(self._today, self._seq)
        self.stream = self._open()

        self._purge_old()

    def _purge_old(self):
        if self._keepDays <= 0:
            return
        cutoff = (datetime.now() - timedelta(days=self._keepDays)).strftime("%Y-%m-%d")
        pattern = re.compile(
            re.escape(self._stem) + r"\.(\d{4}-\d{2}-\d{2})\.\d+\.log$"
        )
        for path in glob.glob(self._glob_pattern()):
            m = pattern.search(os.path.basename(path))
            if m and m.group(1) < cutoff:
                try:
                    os.remove(path)
                except OSError as exc:
                    logging.getLogger("dream").warning(
                        "Could not remove expired dream log %s: %s", path, exc
                    )


class _StreamToLogger:
    """Redirect writes (e.g. print()) to a logging.Logger."""

    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        self._logger = logger
        self._level = level
        self._buf = ""
        self.encoding = "utf-8"
        self.errors = "replace"

    def write(self, msg: str) -> int:
        if msg:
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line:
                    self._logger.log(self._level, line)
        return len(msg)

    def flush(self):
        if self._buf:
            self._logger.log(self._level, self._buf)
            self._buf = ""

    def reconfigure(self, **kwargs):
        # Called by the UTF-8 fix in __main__ blocks; safe to ignore since
        # we already handle encoding via the logger.
        pass


def setup_logging(
    log_file: str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    keep_days: int = 30,
):
    """Configure logging with timestamped output to console and optionally a file.

    After calling this, all print() output goes through the logger with
    ISO timestamps. stderr is also captured at WARNING level.
    """
    logger = logging.getLogger("dream")
    logger.setLevel(logging.DEBUG)

    # Console handler — always present, UTF-8.
    console = logging.StreamHandler(stream=open(
        sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False
    ))
    console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    logger.addHandler(console)

    # File handler — if log_file is specified.
    if log_file:
        fh = DreamFileHandler(_resolve_log_path(log_file),
                              maxBytes=max_bytes, keepDays=keep_days)
        fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
        logger.addHandler(fh)

    # Redirect stdout/stderr to the logger so plain print() gets timestamps.
    sys.stdout = _StreamToLogger(logger, logging.INFO)
    sys.stderr = _StreamToLogger(logger, logging.WARNING)
