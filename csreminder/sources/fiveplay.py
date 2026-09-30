"""5EPlay 中文资讯源（国内可直连，走官方 JSON 接口）。

接口: https://csgo.5eplay.com/api/article/index
返回: {"data": {"count": 30, "list": [{"title","dateline","jump_link","hits",...}]}}
失败时回退到解析 https://csgo.5eplay.com/article 的 HTML 列表。
"""
from __future__ import annotations

import html as _html
import json
import re

from .. import web

API = "https://csgo.5eplay.com/api/article/index"
LIST_URL = "https://csgo.5eplay.com/article"
BASE = "https://csgo.5eplay.com"

# 明显不属于 CS 的栏目，过滤掉
EXCLUDE = (
    "无畏契约", "英雄联盟", "LPL", "LCK", "KPL", "王者荣耀", "和平精英",
    "永劫无间", "DOTA", "Dota", "dota", "守望先锋", "PUBG", "绝地求生",
    "云顶之弈", "金铲铲", "魔兽世界", "炉石",
)

# HTML 回退解析
RE_ITEM = re.compile(
    r'<a href="(https?://csgo\.5eplay\.com/article/[^"]+)"[^>]*>.*?'
    r'<p class="video-title">([^<]+)</p>.*?'
    r'<p class="video-date">([^<]+)</p>',
    re.I | re.S,
)
RE_TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return _html.unescape(RE_TAG.sub("", s or "")).strip()


def _relevant(title: str, cfg) -> bool:
    return bool(cfg.find_teams(title))


def fetch_news(cfg) -> dict:
    conf = cfg.sources.get("fiveplay", {})
    if not conf.get("enabled", True):
        return {"items": [], "ok": False, "error": "5EPlay 已禁用"}
    limit = int(conf.get("news_limit", 8))

    items, err = [], ""
    try:
        text = web.request(API, referer=LIST_URL,
                           headers={"X-Requested-With": "XMLHttpRequest"},
                           timeout=25, retries=2)
        data = json.loads(text)
        raw = ((data.get("data") or {}).get("list")) or []
        for it in raw:
            title = (it.get("title") or "").strip()
            if not title or any(k in title for k in EXCLUDE):
                continue
            items.append({
                "title": title,
                "url": it.get("jump_link") or LIST_URL,
                "date": int(it.get("dateline") or 0),
                "hits": it.get("hits"),
                "source": "5EPlay",
            })
    except Exception as e:
        err = "5EPlay 接口失败(%s)，尝试 HTML 回退" % type(e).__name__
        try:
            page = web.request(LIST_URL, referer=BASE + "/", timeout=30, retries=1)
            for m in RE_ITEM.finditer(page):
                items.append({"title": _clean(m.group(2)), "url": m.group(1),
                              "date": 0, "source": "5EPlay"})
        except Exception as e2:
            return {"items": [], "ok": False,
                    "error": "5EPlay 抓取失败: %s / %s" % (e, e2)}

    # 关注战队相关排前面
    items.sort(key=lambda x: (0 if _relevant(x["title"], cfg) else 1, -x.get("date", 0)))
    return {"items": items[:limit], "ok": True, "error": err}


def fetch_schedule_hint(cfg) -> dict:
    """标题里带赛程/对阵/开赛字样的资讯，作为结构化赛程的补充。"""
    res = fetch_news(cfg)
    if not res.get("ok"):
        return res
    keys = ("赛程", "对阵", "开赛", "出征", "晋级", "小组", "淘汰赛", "Major", "IEM", "BLAST", "ESL")
    return {"items": [i for i in res["items"] if any(k in i["title"] for k in keys)],
            "ok": True, "error": res.get("error", "")}
