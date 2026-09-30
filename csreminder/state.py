"""已推送记录，避免重复打扰。"""
from __future__ import annotations

import json
import os
import tempfile
import time


class State:
    def __init__(self, path, keep_days=30):
        self.path = path
        self.keep_days = keep_days
        self.data = {"sent": {}}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {"sent": {}}
        self.data.setdefault("sent", {})

    def already_sent(self, key):
        return key in self.data["sent"]

    def mark_sent(self, key, extra=None):
        self.data["sent"][key] = {"at": int(time.time()), **(extra or {})}

    def prune(self):
        cutoff = time.time() - self.keep_days * 86400
        self.data["sent"] = {k: v for k, v in self.data["sent"].items()
                             if v.get("at", 0) >= cutoff}

    def save(self):
        self.prune()
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
