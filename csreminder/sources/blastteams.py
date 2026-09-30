"""BLAST.tv 战队 / 选手 数据源。

数据来自 BLAST 官方战队页的服务端渲染 HTML：
    https://blast.tv/cs/team/<id>/<slug>

页面里有一句信息量极大的摘要：
    TYLOO is a pro Counter-Strike team from China. Their current roster is
    Jee, JamYoung, zero, Mercury and Moseyuh. They are currently 26th in the
    Global Valve Rankings with 1364 points.
再配合 Core Stats 卡片（场次 / 胜负 / 地图）和 Roster 区，就是一份完整的战队档案。
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .. import web

BASE = "https://blast.tv"
TEAM_LIST_URL = BASE + "/cs/team"
INDEX_TTL = 86400 * 3          # 战队索引缓存 3 天
PLAYER_TTL = 86400 * 3         # 选手索引缓存 3 天

BAD_NAMES = {
    "maps", "matches", "matches w/l", "maps w/l", "score", "opponent", "result",
    "roster", "tournaments", "prize", "record", "place", "date", "kills", "deaths",
    "adr", "headshot %", "first kill", "core stats", "rating", "kd", "k/d",
    "trophy cabinet", "roster history", "best placements", "show more", "see all",
}

RE_SUMMARY = re.compile(
    r"([^<>]{2,40}?) is a pro Counter-Strike team from ([^.<]{2,40})\."
    r"(?: Their current roster is ([^.<]{2,200})\.)?"
    r"(?: They are currently ([^.<]{2,60}?) in the ([^.<]{2,60}?) with (\d+) points\.)?")
RE_PLAYER_LINK = re.compile(r'href="/cs/player/([0-9a-f]{6,12})/([a-z0-9\-]+)"')
RE_TEAM_LINK = re.compile(r'/cs/team/([0-9a-f]{6,12})/([a-z0-9\-]+)')
RE_COUNTRY = re.compile(r'<img title="([A-Za-z][A-Za-z ()\-]{2,30})"')
RE_STAT = re.compile(
    r'text-neutral">([A-Za-z /%]+)</span>.*?heading-h4[^>]*">([^<]+)</span>'
    r'(?:.*?body-b4[^"]*">(\d+%)</span>)?', re.S)
RE_RECENT = re.compile(
    r'(\d{4}-\d{2}-\d{2})</[^>]+>.*?R</[^>]+>.*?(?:<[^>]+>\s*)*?(\d+)\s*:\s*(\d+)'
    r'.*?<[^>]+>([^<]{2,30})</', re.S)

CN_TEAM = {}


def _data_dir() -> str:
    from ..config import BASE_DIR
    d = os.path.join(BASE_DIR, "data")
    os.makedirs(d, exist_ok=True)
    return d


def _load_cache(name: str, ttl: int):
    p = os.path.join(_data_dir(), name)
    try:
        if time.time() - os.path.getmtime(p) < ttl:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_cache(name: str, obj):
    try:
        with open(os.path.join(_data_dir(), name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def _fetch(url: str, ref: str = BASE + "/cs") -> str:
    return web.request(url, referer=ref, timeout=30, retries=2)


# ---------------------------------------------------------------- 战队索引
def load_team_index(force: bool = False) -> dict:
    """{slug: {"id":..., "slug":...}}，来源：排名页 + 各赛事页。"""
    if not force:
        c = _load_cache("team_index.json", INDEX_TTL)
        if c:
            return c

    found = {}
    try:
        html = _fetch(TEAM_LIST_URL)
        for tid, slug in RE_TEAM_LINK.findall(html):
            found[slug] = {"id": tid, "slug": slug}
    except Exception:
        pass

    # 再用各赛事页补充（含更多战队）
    try:
        from . import blasttv
        for tslug in (blasttv.list_tournaments() or [])[:10]:
            try:
                h = _fetch("%s/cs/tournaments/%s" % (BASE, tslug))
                for tid, slug in RE_TEAM_LINK.findall(h):
                    found.setdefault(slug, {"id": tid, "slug": slug})
            except Exception:
                continue
    except Exception:
        pass

    if found:
        _save_cache("team_index.json", found)
    return found


def find_team(cfg, query: str):
    """按队名找战队，返回 {"id","slug","name"} 或 None。"""
    q = (query or "").strip().lower()
    if not q:
        return None
    idx = load_team_index()
    if not idx:
        return None

    # 1) slug 精确 / 包含
    for slug, v in idx.items():
        if q == slug or q == slug.replace("-", " "):
            return dict(v, name=slug)
    # 2) 关注队伍的别名表（含中文绰号：天禄 / 小蜜蜂 / 绿龙）
    for t in cfg.teams:
        if t.matches(q):
            wants = [
                t.name.lower().replace(" ", "-"),
                t.name.lower().replace(" ", ""),
                t.name.lower().split()[-1],
            ]
            for want in wants:
                for slug, v in idx.items():
                    if slug == want or slug.replace("-", "") == want.replace("-", ""):
                        return dict(v, name=slug)
    # 3) 模糊包含
    cands = [(slug, v) for slug, v in idx.items()
             if q in slug.replace("-", " ") or slug.replace("-", " ") in q]
    if cands:
        cands.sort(key=lambda x: len(x[0]))
        slug, v = cands[0]
        return dict(v, name=slug)
    return None


# ---------------------------------------------------------------- 页面解析
def _text(html: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", html)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", "|", s)
    s = s.replace("&#x27;", "'").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\|{2,}", "|", s)


def parse_team(html: str, fallback_name: str = "") -> dict:
    out = {"name": fallback_name, "country": "", "rank": "", "ranking_body": "",
           "points": "", "roster": [], "stats": {}, "recent": [], "tournaments": []}

    m = RE_SUMMARY.search(html)
    if m:
        out["name"] = (m.group(1) or fallback_name).strip()
        out["country"] = (m.group(2) or "").strip()
        if m.group(3):
            out["roster_names"] = [x.strip() for x in re.split(r",| and ", m.group(3)) if x.strip()]
        if m.group(4):
            out["rank"] = m.group(4).strip()
            out["ranking_body"] = (m.group(5) or "").strip()
            out["points"] = m.group(6)

    # 阵容（带选手 id、国籍、真名）
    roster = []
    links = [(m.start(), m.group(1), m.group(2))
             for m in re.finditer(r'href="/cs/player/([0-9a-f]{6,12})/([a-z0-9\-]+)"', html)]
    for i2, (pos, pid, slug) in enumerate(links):
        end = links[i2 + 1][0] if i2 + 1 < len(links) else min(len(html), pos + 1500)
        blk = html[pos:end]
        # 最后一个选手后面会跟到 Core Stats，切掉
        for stop in ("Core Stats", "Trophy Cabinet", "Roster History"):
            cut = blk.find(stop)
            if cut > 0:
                blk = blk[:cut]
        alt = re.search(r'alt="([^"]{1,30})"', blk)
        ctry = re.search(r'<img title="([A-Za-z][A-Za-z ()\-]{2,30})"', blk)
        nickname = (alt.group(1) if alt else slug).strip()
        real = ""
        for sp in reversed(re.findall(r"<span[^>]*>([\s\S]{0,120}?)</span>", blk)):
            t = re.sub(r"<!--[\s\S]*?-->", " ", sp)
            t = re.sub(r"<[^>]+>", " ", t)
            t = re.sub(r"\s+", " ", t).strip()
            if not t or t.lower() == nickname.lower():
                continue
            if t.lower() in BAD_NAMES:
                continue
            if "Statistic" in t or "Counter-Strike" in t or "&#" in t or "&" in t:
                continue
            # 真名长得像 "Ji Dongkai" / "Danil Kryshkovets"：1~3 个首字母大写的词
            if not re.fullmatch(r"[A-Z][A-Za-z'\-]{1,15}(?: [A-Z][A-Za-z'\-]{1,15}){0,2}", t):
                continue
            real = t
            break
        roster.append({
            "id": pid, "slug": slug, "name": nickname,
            "country": (ctry.group(1) if ctry else "").strip(),
            "real": real,
        })
    # 去重（同一选手可能在页面出现多次）
    seen, uniq = set(), []
    for r in roster:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    out["roster"] = uniq[:8]
    if not out["roster"] and out.get("roster_names"):
        out["roster"] = [{"id": "", "slug": n.lower(), "name": n, "country": "", "real": ""}
                         for n in out["roster_names"]]

    # Core Stats
    for m3 in re.finditer(
            r'text-neutral">([^<]{2,30})</span>[\s\S]{0,200}?heading-h4[^>]*">([^<]{1,20})</span>'
            r'(?:[\s\S]{0,120}?body-b4[^"]*">(\d+%)</span>)?', html):
        label, val, pct = m3.group(1).strip(), m3.group(2).strip(), m3.group(3)
        if label and label not in out["stats"]:
            out["stats"][label] = val + ((" (" + pct + ")") if pct else "")

    # 近期 / 未来比赛：日期 -> 比分 -> 对手队名
    for m4 in re.finditer(
            r'<div[^>]*>(\d{4}-\d{2}-\d{2})</div>[\s\S]{0,400}?<a[^>]*>\s*(\d+)\s*:\s*(\d+)\s*</a>'
            r'[\s\S]{0,900}?<span[^>]*>([^<]{2,30})</span>', html):
        d, a, b, opp = m4.group(1), m4.group(2), m4.group(3), m4.group(4).strip()
        if opp.lower().endswith("team logo") or opp in ("Score", "Opponent", "Result"):
            continue
        item = [d, "%s : %s" % (a, b), opp]
        if item not in out["recent"]:
            out["recent"].append(item)
    out["recent"] = out["recent"][:8]

    # 参赛赛事
    for m5 in re.finditer(r'href="/cs/tournaments/([a-z0-9\-]{4,60})"[^>]*>([^<]{3,60})</a>', html):
        slug_t, label = m5.group(1), m5.group(2).strip()
        if "/match/" in slug_t or label in ("Score", "Opponent"):
            continue
        if label not in out["tournaments"]:
            out["tournaments"].append(label)
    out["tournaments"] = out["tournaments"][:6]
    return out


def fetch_team(url_slug: str) -> dict:
    """url_slug 形如 '1cdfe400/tyloo' 或 'tyloo'。"""
    if "/" not in url_slug:
        idx = load_team_index()
        v = idx.get(url_slug)
        if not v:
            return {}
        url_slug = "%s/%s" % (v["id"], v["slug"])
    html = _fetch("%s/cs/team/%s" % (BASE, url_slug))
    return parse_team(html, url_slug.split("/")[-1])


# ---------------------------------------------------------------- 选手索引
def load_player_index(cfg, force: bool = False) -> dict:
    """{选手小写名: {"name","slug","team","team_slug","country","real"}}"""
    if not force:
        c = _load_cache("players.json", PLAYER_TTL)
        if c:
            return c

    idx = load_team_index()
    # 关注队伍优先
    priority = []
    for t in cfg.teams:
        want = t.name.lower().replace(" ", "-")
        for slug, v in idx.items():
            if slug == want or slug.replace("-", "") == want.replace("-", ""):
                priority.append(slug)
    rest = [s for s in idx if s not in priority]
    order = priority + rest[:45]

    players = {}

    def one(slug):
        v = idx[slug]
        try:
            team = fetch_team("%s/%s" % (v["id"], v["slug"]))
        except Exception:
            return []
        rows = []
        for p in team.get("roster", []):
            rows.append((p["name"].lower(), {
                "name": p["name"], "slug": p["slug"], "country": p["country"],
                "real": p.get("real", ""), "team": team.get("name") or slug,
                "team_slug": "%s/%s" % (v["id"], v["slug"]),
                "team_country": team.get("country", ""),
                "team_rank": team.get("rank", ""),
                "team_points": team.get("points", ""),
            }))
        return rows

    with ThreadPoolExecutor(max_workers=8) as ex:
        for rows in ex.map(one, order):
            for k, v in rows:
                players.setdefault(k, v)

    if players:
        _save_cache("players.json", players)
    return players


def find_player(cfg, query: str):
    q = (query or "").strip().lower()
    if not q:
        return None
    pi = load_player_index(cfg)
    if q in pi:
        return pi[q]
    cands = [(k, v) for k, v in pi.items() if q in k or k in q]
    if cands:
        cands.sort(key=lambda x: len(x[0]))
        return cands[0][1]
    return None
