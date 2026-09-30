"""Steam 官方 CS2 更新 / 新闻。"""
from __future__ import annotations

from .. import web

URL = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
       "?appid=730&count=%d&format=json&maxlength=300")


def fetch_news(cfg) -> dict:
    conf = cfg.sources.get("steam", {})
    if not conf.get("enabled", True):
        return {"items": [], "ok": False, "error": "Steam 新闻已禁用"}
    limit = int(conf.get("news_limit", 5))
    try:
        j = web.get_json(URL % max(limit, 5), timeout=25, retries=2)
        raw = (j.get("appnews") or {}).get("newsitems") or []
    except Exception as e:
        return {"items": [], "ok": False, "error": "Steam 新闻抓取失败: %s" % e}

    items = []
    for it in raw[:limit]:
        items.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("url") or "",
            "date": it.get("date") or 0,
            "source": "Steam",
        })
    return {"items": items, "ok": True, "error": ""}
