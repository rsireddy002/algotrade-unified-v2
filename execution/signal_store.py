"""
execution/signal_store.py — Persistent JSON signal log with dedup.

fno-scanner-strategy-update noted signal deduplication issues in the
existing system. This addresses that directly: every signal is keyed by
(ticker, signal_time) — the same de-dup key already proven to work
correctly in breakout-scanner-streamlit's alerted_signals.json — and
writes are atomic (write to a temp file, then rename) so a crash
mid-write can't corrupt the log the way a direct overwrite could.
"""

import json
import os
import tempfile


class SignalStore:
    def __init__(self, filepath):
        self.filepath = filepath
        self._seen_keys = set()
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._seen_keys = set(tuple(k) for k in data.get("seen_keys", []))
        else:
            self._seen_keys = set()

    def _save(self):
        # Atomic write: write to a temp file in the same directory, then
        # rename over the target — rename is atomic on both POSIX and
        # Windows (for files on the same volume), so a crash mid-write
        # leaves either the old file intact or the new one complete, never
        # a half-written corrupt file.
        directory = os.path.dirname(os.path.abspath(self.filepath)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"seen_keys": [list(k) for k in self._seen_keys]}, f)
            os.replace(tmp_path, self.filepath)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def is_duplicate(self, ticker, signal_time):
        return (ticker, signal_time) in self._seen_keys

    def record(self, ticker, signal_time):
        """Mark (ticker, signal_time) as seen and persist immediately.
        Returns True if this was a new signal, False if it was already
        recorded (i.e. a duplicate that should be skipped)."""
        key = (ticker, signal_time)
        if key in self._seen_keys:
            return False
        self._seen_keys.add(key)
        self._save()
        return True

    def __len__(self):
        return len(self._seen_keys)
