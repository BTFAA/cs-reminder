"""BLAST.tv 赛程数据源（国内可直连，无需注册、无需 API key）。

数据来自 BLAST 官方赛事页的服务端渲染 HTML：
    https://blast.tv/cs/tournaments/<tournament-slug>
每个比赛卡片长这样：
    <a href="/cs/tournaments/<tour>/match/<id>/<slug>">
      Group Stage · Match 3
      Team Falcons  <time dateTime="2026-10-03T08:00:00.000Z">08:00</time>  TYLOO
    </a>
"""
from __future__ import annotations

import re

from .. import web

BASE = "https://blast.tv"
LIST_URL = BASE + "/cs/tournaments"

# 比赛卡片
RE_ANCHOR = re.compile(r'<a href="/cs/tournaments/([^"/]+)/match/([^"/]+)/[^"]*"')
RE_TEAM_A = re.compile(r'<span[^>]*>([^<]{2,32})</span>\s*<img[^>]*alt="([^"]+)"')
RE_TEAM_B = re.compile(r'<img[^>]*alt="([^"]+)"\s*/?>\s*<span[^>]*>([^<]{2,32})</span>')
RE_TIME = re.compile(r'<time dateTime="([^"]+)"')
RE_STAGE = re.compile(r'<span[^>]*>([A-Za-z][A-Za-z ]{2,28})(?:<!-- -->)?\s*·\s*</span>')

# 赛事 slug -> 等级（依据赛事品牌判断）
TIER_RULES = [
    ("major", "s"), ("iem", "s"), ("esl-pro-league", "s"),
    ("blast-premier", "s"), ("pgl-masters", "s"), ("blast-world-final", "s"),
    ("blast-open", "a"), ("fissure", "a"), ("esl-challenger", "b"),
]
TIER_CN = {"s": "S 级（顶级赛事）", "a": "A 级", "b": "B 级", "c": "C 级"}

# 赛事 slug 里的城市名 -> 中文地点
CITY = {
    "beijing": "中国 北京", "china": "中国", "shanghai": "中国 上海",
    "chengdu": "中国 成都", "porto": "葡萄牙 波尔图", "bucharest": "罗马尼亚 布加勒斯特",
    "hongkong": "中国 香港", "rio": "巴西 里约", "paris": "法国 巴黎",
    "london": "英国 伦敦", "copenhagen": "丹麦 哥本哈根", "katowice": "波兰 卡托维兹",
    "dallas": "美国 达拉斯", "singapore": "新加坡", "seoul": "韩国 首尔",
}


def _tier(slug: str) -> str:
    s = slug.lower()
    for key, tier in TIER_RULES:
        if key in s:
            return tier
    return ""


def _location(slug: str) -> str:
    s = slug.lower()
    for key, loc in CITY.items():
        if key in s:
            return loc
    return ""


def _pretty(slug: str) -> str:
    """esl-pro-league-season-24-2026 -> ESL Pro League Season 24 2026"""
    s = slug.replace("-", " ").strip()
    fixed = {"esl": "ESL", "iem": "IEM", "pgl": "PGL", "blast": "BLAST", "cs": "CS"}
    words = []
    for w in s.split():
        words.append(fixed.get(w.lower(), w.capitalize() if not w.isdigit() else w))
    return " ".join(words)


def _parse(html_text: str, tournament_slug: str) -> list:
    anchors = [(m.start(), m.group(2)) for m in RE_ANCHOR.finditer(html_text)]
    out = []
    for idx, (pos, mid) in enumerate(anchors):
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else min(len(html_text), pos + 6000)
        blk = html_text[pos:end]
        a = RE_TEAM_A.search(blk)
        b = RE_TEAM_B.search(blk)
        tm = RE_TIME.search(blk)
        if not (a and b and tm):
            continue
        st = RE_STAGE.search(blk)
        tier = _tier(tournament_slug)
        out.append({
            "id": "blast:%s" % mid,
            "name": "%s vs %s" % (a.group(2), b.group(1)),
            "begin_at": tm.group(1),
            "team_a": a.group(2),
            "team_b": b.group(1),
            "teams": [a.group(2), b.group(1)],
            "tournament": _pretty(tournament_slug),
            "stage": st.group(1).strip() if st else "",
            "tier": tier,
            "tier_cn": TIER_CN.get(tier, ""),
            "region": "",
            "location": _location(tournament_slug),
            "bo": None,
            "status": "not_started",
            "streams": [],
            "url": "%s/cs/tournaments/%s/match/%s" % (BASE, tournament_slug, mid),
            "source": "BLAST.tv",
        })
    return out


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
YEAR_RE = re.compile(r"(20\d\d)")


def list_tournaments() -> list:
    """从赛事列表页拿到真正可用的赛事 slug（滤掉 UUID，按年份从新到旧）。"""
    try:
        html_text = web.request(LIST_URL, referer=BASE + "/cs", timeout=30, retries=2)
    except Exception:
        return []

    raw = set(re.findall(r'/cs/tournaments/([a-z0-9][a-z0-9\-]{3,60})', html_text))
    raw |= set(re.findall(r'/images/tournament/([a-z0-9][a-z0-9\-]{3,60})', html_text))

    good = []
    for s in raw:
        if UUID_RE.match(s):          # 纯 UUID 不是赛事名
            continue
        if "-" not in s or len(s) < 8:
            continue
        good.append(s)

    import datetime
    now_year = datetime.datetime.now().year

    # 年份离当前越近越靠前（2026 优先于 2027/2025），没年份的排最后
    def key(s):
        m = YEAR_RE.search(s)
        if not m:
            return (1, 0, s)
        return (0, abs(int(m.group(1)) - now_year), s)
    return sorted(good, key=key)


def fetch_all(cfg) -> dict:
    conf = cfg.sources.get("blasttv", {})
    if not conf.get("enabled", True):
        return {"upcoming": [], "past": [], "ok": False, "error": "BLAST.tv 已禁用"}

    max_t = int(conf.get("max_tournaments", 12))
    slugs = conf.get("tournaments") or list_tournaments()
    if not slugs:
        return {"upcoming": [], "past": [], "ok": False,
                "error": "BLAST.tv 赛事列表抓取失败"}

    matches, errors = [], []
    for slug in slugs[:max_t]:
        try:
            html_text = web.request("%s/cs/tournaments/%s" % (BASE, slug),
                                    referer=LIST_URL, timeout=30, retries=1)
            matches.extend(_parse(html_text, slug))
        except Exception as e:
            errors.append("%s: %s" % (slug, type(e).__name__))

    # 去重
    seen, uniq = set(), []
    for m in matches:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        uniq.append(m)

    err = ""
    if errors:
        err = "部分赛事抓取失败: " + ", ".join(errors[:3])
    if not uniq:
        return {"upcoming": [], "past": [], "ok": False,
                "error": err or "BLAST.tv 没抓到任何比赛（可能赛事列表为空）"}
    return {"upcoming": uniq, "past": [], "ok": True, "error": err}
