"""配置加载（支持从环境变量读取凭据，用于云端部署）。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")


@dataclass
class Team:
    name: str
    cn: str = ""
    aliases: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return (self.cn + " " + self.name).strip() if self.cn else self.name

    def matches(self, text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        for a in [self.name] + list(self.aliases):
            if a and a.lower() in low:
                return True
        return False


@dataclass
class Config:
    path: str
    raw: dict
    teams: list

    @property
    def sources(self) -> dict:
        return self.raw.get("sources", {})

    @property
    def notify(self) -> dict:
        return self.raw.get("notify", {})

    @property
    def rules(self) -> dict:
        return self.raw.get("rules", {})

    @property
    def output(self) -> dict:
        return self.raw.get("output", {})

    @property
    def tz_name(self) -> str:
        return self.output.get("timezone", "Asia/Shanghai")

    def enabled_channels(self) -> list:
        return [c for c in self.notify.get("channels", []) if c.get("enabled")]

    def find_teams(self, text: str) -> list:
        return [t for t in self.teams if t.matches(text)]


def _apply_env(raw: dict) -> dict:
    """云端部署：凭据从环境变量读（GitHub Secrets），绝不写进仓库。

    支持的环境变量：
        QQ_APPID / QQ_SECRET / QQ_OPENID / QQ_GROUP_OPENID
        PANDASCORE_TOKEN（可选）
    """
    notify = raw.setdefault("notify", {})
    chans = notify.setdefault("channels", [])

    ch = next((c for c in chans if c.get("type") == "qqofficial"), None)
    if ch is None:
        ch = {"type": "qqofficial"}
        chans.insert(0, ch)

    for env_key, field_name in (
        ("QQ_APPID", "appId"),
        ("QQ_SECRET", "clientSecret"),
        ("QQ_OPENID", "user_openid"),
        ("QQ_GROUP_OPENID", "group_openid"),
    ):
        v = os.environ.get(env_key)
        if v:
            ch[field_name] = v.strip()

    if ch.get("appId") and ch.get("clientSecret"):
        ch["enabled"] = True
        for c in chans:
            if c is not ch:
                c["enabled"] = False

    token = os.environ.get("PANDASCORE_TOKEN")
    if token:
        raw.setdefault("sources", {}).setdefault("pandascore", {})["token"] = token.strip()

    return raw


def load(path=None) -> Config:
    path = path or os.environ.get("CS_REMINDER_CONFIG") or DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw = _apply_env(raw)
    teams = [
        Team(name=t["name"], cn=t.get("cn", ""), aliases=t.get("aliases", []))
        for t in raw.get("teams", [])
    ]
    if not teams:
        raise ValueError(path + " 里没有配置任何关注队伍（teams 为空）")
    return Config(path=path, raw=raw, teams=teams)
