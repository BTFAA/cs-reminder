"""PandaScore 赛事数据源（结构化：时间 / 赛事等级 / 地区 / 对阵）。

免费 token：https://app.pandascore.co/signup 注册后 Account 页复制。
"""
from __future__ import annotations

from .. import web

API = "https://api.pandascore.co"

# PandaScore 的赛事等级 -> 中文
TIER_CN = {
    "s": "S 级（顶级赛事）",
    "a": "A 级",
    "b": "B 级",
    "c": "C 级",
    "d": "D 级",
    "unranked": "未分级",
}


class MissingToken(RuntimeError):
    pass


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def _text(*vals) -> str:
    for v in vals:
        if v:
            return str(v)
    return ""


def _parse_match(m: dict) -> dict:
    opps = m.get("opponents") or []
    names = []
    for o in opps:
        opp = o.get("opponent") or {}
        names.append(opp.get("name") or opp.get("acronym") or "")
    t = m.get("tournament") or {}
    lg = m.get("league") or {}
    ser = m.get("serie") or {}
    tier = (t.get("tier") or "").lower()

    # 地点：PandaScore 的赛事对象不一定都有，尽量多取几个可能字段
    location = _text(t.get("location"), t.get("country"), ser.get("full_name") if False else "")
    region = _text(t.get("region"), lg.get("region"), ser.get("region"))

    streams = [s.get("raw_url") or s.get("main") or "" for s in (m.get("streams_list") or [])]
    streams = [s for s in streams if s]

    return {
        "id": m.get("id"),
        "name": m.get("name") or "",
        "begin_at": m.get("begin_at") or m.get("scheduled_at") or "",
        "team_a": names[0] if len(names) > 0 else "",
        "team_b": names[1] if len(names) > 1 else "",
        "teams": [n for n in names if n],
        "tournament": _text(t.get("name"), lg.get("name")),
        "tier": tier,
        "tier_cn": TIER_CN.get(tier, tier.upper() + " 级" if tier else ""),
        "region": region,
        "location": location,
        "bo": m.get("number_of_games"),
        "status": m.get("status") or "",
        "streams": streams[:2],
        "source": "PandaScore",
    }


def _fetch(path: str, token: str, **params) -> list:
    q = "&".join("%s=%s" % (k, v) for k, v in params.items())
    url = API + path + ("?" + q if q else "")
    data = web.get_json(url, headers=_auth(token), timeout=30, retries=2)
    if not isinstance(data, list):
        return []
    return [_parse_match(m) for m in data]


def fetch_upcoming(token: str, videogame: str = "csgo", per_page: int = 100) -> list:
    return _fetch("/%s/matches/upcoming" % videogame, token,
                  per_page=per_page, sort="begin_at")


def fetch_past(token: str, videogame: str = "csgo", per_page: int = 100) -> list:
    return _fetch("/%s/matches/past" % videogame, token,
                  per_page=per_page, sort="-begin_at")


def fetch_all(cfg) -> dict:
    """返回 {"upcoming": [...], "past": [...], "ok": bool, "error": str}"""
    conf = cfg.sources.get("pandascore", {})
    if not conf.get("enabled", True):
        return {"upcoming": [], "past": [], "ok": False, "error": "pandascore 已禁用"}
    token = (conf.get("token") or "").strip()
    if not token:
        return {"upcoming": [], "past": [], "ok": False,
                "error": "未配置 PandaScore token（config.json -> sources.pandascore.token）"}
    vg = conf.get("videogame", "csgo")
    try:
        return {"upcoming": fetch_upcoming(token, vg),
                "past": fetch_past(token, vg),
                "ok": True, "error": ""}
    except web.HttpError as e:
        hint = ""
        if e.status in (401, 403):
            hint = "（token 无效或过期，请到 app.pandascore.co 重新复制）"
        return {"upcoming": [], "past": [], "ok": False,
                "error": "PandaScore HTTP %s %s" % (e.status, hint)}
    except Exception as e:
        return {"upcoming": [], "past": [], "ok": False,
                "error": "PandaScore 请求失败: %s" % e}
