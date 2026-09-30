"""上下文记忆：记住每个会话（私聊/群）上一句问的是谁。

存在 data/context.json，带过期时间（默认 30 分钟）。
"""
from __future__ import annotations

import json
import os
import time

TTL = 1800


def _path() -> str:
    from .config import BASE_DIR
    d = os.path.join(BASE_DIR, "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "context.json")


def _load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: dict):
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def get(target: str) -> dict:
    """取某个会话的上下文，过期返回空。"""
    if not target:
        return {}
    d = _load()
    e = d.get(target) or {}
    if e and time.time() - e.get("ts", 0) > TTL:
        return {}
    return e


def put(target: str, kind: str, name: str, intent: str = ""):
    """记下这个会话刚问过谁。"""
    if not (target and name):
        return
    d = _load()
    d[target] = {"kind": kind, "name": name, "intent": intent, "ts": time.time()}
    # 顺手清理过期项，别让文件无限膨胀
    now = time.time()
    d = {k: v for k, v in d.items() if now - v.get("ts", 0) <= TTL * 4}
    _save(d)
