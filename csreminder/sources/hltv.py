"""HLTV 数据源（通过 r.jina.ai 抓取）。

HLTV 官方站对数据中心 IP 一律 403（Cloudflare），本机在国内也连不上。
但 r.jina.ai 这个阅读代理用自己的出口去抓，能正常拿到页面并转成干净的 Markdown，
所以这里统一走它。

提供：
    ranking()            -> HLTV 世界排名（前 30，含积分和阵容）
    team_rank(name)      -> 某队的 HLTV 排名
    player_stats(slug)   -> 选手详细数据（Rating / Firepower / 年龄 / 奖金 / 荣誉）
"""
from __future__ import annotations

import json
import os
import re
import time

from .. import web

JINA = "https://r.jina.ai/"
HLTV = "https://www.hltv.org/"
CACHE_TTL = 3600 * 6          # 排名 6 小时
PLAYER_TTL = 3600 * 24        # 选手 1 天

RE_RANK_ENTRY = re.compile(
    r"#(\d+)[\s\S]{0,600}?([A-Za-z0-9][A-Za-z0-9 .'\-]{1,28}?)\((\d+)\s*HLTV points\)")
RE_TEAM_LINK = re.compile(r"hltv\.org/team/(\d+)/([a-z0-9\-]+)")

CC = {
    "RU": "俄罗斯", "UA": "乌克兰", "BY": "白俄罗斯", "KZ": "哈萨克斯坦",
    "CN": "中国", "MN": "蒙古", "KR": "韩国", "JP": "日本", "IN": "印度",
    "ID": "印度尼西亚", "VN": "越南", "TH": "泰国", "PH": "菲律宾",
    "FR": "法国", "DE": "德国", "GB": "英国", "SE": "瑞典", "DK": "丹麦",
    "NO": "挪威", "FI": "芬兰", "PL": "波兰", "CZ": "捷克", "SK": "斯洛伐克",
    "HU": "匈牙利", "RO": "罗马尼亚", "BG": "保加利亚", "RS": "塞尔维亚",
    "HR": "克罗地亚", "SI": "斯洛文尼亚", "BA": "波黑", "MK": "北马其顿",
    "LT": "立陶宛", "LV": "拉脱维亚", "EE": "爱沙尼亚", "MD": "摩尔多瓦",
    "GE": "格鲁吉亚", "AM": "亚美尼亚", "AZ": "阿塞拜疆", "IL": "以色列",
    "TR": "土耳其", "BR": "巴西", "AR": "阿根廷", "CL": "智利", "PE": "秘鲁",
    "UY": "乌拉圭", "CO": "哥伦比亚", "US": "美国", "CA": "加拿大",
    "MX": "墨西哥", "AU": "澳大利亚", "NZ": "新西兰", "ZA": "南非",
    "ES": "西班牙", "PT": "葡萄牙", "IT": "意大利", "NL": "荷兰",
    "BE": "比利时", "CH": "瑞士", "AT": "奥地利", "IE": "爱尔兰",
    "GR": "希腊", "TW": "中国台湾", "HK": "中国香港", "SG": "新加坡",
    "MY": "马来西亚", "JO": "约旦", "SA": "沙特阿拉伯", "AE": "阿联酋",
    "EG": "埃及", "MA": "摩洛哥", "TN": "突尼斯", "PK": "巴基斯坦",
}


def _data_dir() -> str:
    from ..config import BASE_DIR
    d = os.path.join(BASE_DIR, "data")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_get(name: str, ttl: int):
    p = os.path.join(_data_dir(), name)
    try:
        if time.time() - os.path.getmtime(p) < ttl:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _cache_put(name: str, obj):
    try:
        with open(os.path.join(_data_dir(), name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


CAPTCHA_HINT = "Performing security verification"

# r.jina.ai 会拒绝非浏览器 UA，必须伪装成 Chrome
JINA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/plain,text/markdown,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "X-Return-Format": "markdown",
}


def _curl_get(url: str, timeout: int = 90):
    """用系统 curl 抓取。

    r.jina.ai 对 urllib 的请求返回 403，对 curl 正常，所以优先走 curl。
    （GitHub Actions 的 ubuntu 和 Windows 10+ 都自带 curl）
    """
    import shutil
    import subprocess
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        return None
    cmd = [exe, "-sL", "--compressed", "-m", str(timeout),
           "-A", JINA_HEADERS["User-Agent"],
           "-H", "Accept: " + JINA_HEADERS["Accept"],
           "-H", "Accept-Language: " + JINA_HEADERS["Accept-Language"],
           "-H", "X-Return-Format: markdown",
           "-H", "Referer: https://www.google.com/",
           url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout.decode("utf-8", "replace")


def _jina(path_or_url: str, retries: int = 8) -> str:
    import os as _os
    if _os.environ.get("CS_SKIP_HLTV"):
        raise RuntimeError("已设置 CS_SKIP_HLTV，跳过 HLTV")
    """通过 r.jina.ai 取 HLTV 页面。先用 curl，失败再退回 urllib。"""
    url = path_or_url if path_or_url.startswith("http") else HLTV + path_or_url
    target = JINA + url
    last = ""
    net_fail = 0
    for i in range(retries):
        txt = _curl_get(target, 90)
        if txt is None:
            try:
                txt = web.request(target, timeout=90, retries=1, headers=JINA_HEADERS)
            except Exception as e:
                last = "curl 与 urllib 均失败：%s: %s" % (type(e).__name__, str(e)[:100])
                net_fail += 1
                # 网络根本不通（比如国内连不上 r.jina.ai）就快速放弃，
                # 别把 8 次重试全耗光。验证码那种是「通了但内容不对」，才值得多试。
                if net_fail >= 2:
                    raise RuntimeError("连不上 r.jina.ai（%s）" % last)
                time.sleep(2)
                continue
        if CAPTCHA_HINT not in txt and "Just a moment" not in txt[:300]:
            return txt
        last = "HLTV 返回了人机验证页"
        # r.jina.ai 抓 HLTV 是概率性的，多试几次基本都能过
        time.sleep(4 + (i % 3) * 4)
    raise RuntimeError("抓取 HLTV 失败（%s）" % last)


# ------------------------------------------------------------------ 世界排名
def ranking(force: bool = False) -> list:
    """HLTV 世界排名：[{rank,name,points,id,slug,roster}]"""
    import os as _os
    if _os.environ.get("CS_SKIP_HLTV") and not force:
        return _os.environ["CS_SKIP_HLTV"] != "1" and [] or []
    if not force:
        c = _cache_get("hltv_ranking.json", CACHE_TTL)
        if c:
            return c

    txt = _jina("ranking/teams")
    _ids = _cache_get("hltv_player_ids.json", 86400 * 30) or {}
    out = []
    # 按 "#N ... Team(NN HLTV points)" 分块
    marks = [(m.start(), int(m.group(1))) for m in re.finditer(r"\n#(\d+)\s", txt)]
    if not marks:
        marks = [(m.start(), int(m.group(1))) for m in re.finditer(r"#(\d+)", txt)]
    for idx, (pos, rank) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else min(len(txt), pos + 4000)
        blk = txt[pos:end]
        pm = re.search(r"([A-Za-z0-9][A-Za-z0-9 .'\-]{1,28}?)\((\d+)\s*HLTV points\)", blk)
        if not pm:
            continue
        name, points = pm.group(1).strip(), int(pm.group(2))
        lm = RE_TEAM_LINK.search(blk)
        roster = [p for p in re.findall(r"hltv\.org/player/\d+/([a-z0-9\-]+)\)", blk)][:6]
        for _pid, _s in re.findall(r"hltv\.org/player/(\d+)/([a-z0-9\-]+)", blk):
            _ids.setdefault(_s, _pid)
        out.append({
            "rank": rank, "name": name, "points": points,
            "id": lm.group(1) if lm else "", "slug": lm.group(2) if lm else "",
            "roster": roster,
        })
    # 去重（同队可能在页面出现两次）
    seen, uniq = set(), []
    for t in out:
        k = t["name"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    if _ids:
        _cache_put("hltv_player_ids.json", _ids)
    if uniq:
        _cache_put("hltv_ranking.json", uniq)
    return uniq


def team_rank(name: str, ranking_list=None) -> dict:
    """按队名找 HLTV 排名，返回 {rank,points,name,roster} 或 {}。"""
    if not name:
        return {}
    rows = ranking_list if ranking_list is not None else ranking()
    q = name.strip().lower()
    q2 = q.replace("team ", "")
    for t in rows:
        n = t["name"].lower()
        if q == n or q2 == n or q2 == n.replace("team ", ""):
            return t
    for t in rows:
        n = t["name"].lower()
        if q in n or n in q or q2 in n or n in q2:
            return t
    return {}


# ------------------------------------------------------------------ 选手数据
def player_stats(slug: str, force: bool = False) -> dict:
    """选手详细数据。slug 例如 'donk'。"""
    slug = (slug or "").strip().lower()
    if not slug:
        return {}
    cname = "hltv_player_%s.json" % re.sub(r"[^a-z0-9]", "", slug)
    if not force:
        c = _cache_get(cname, PLAYER_TTL)
        if c:
            return c

    # 先搜选手 id
    txt = _jina("search?query=" + slug) if False else None
    pid = _player_id(slug)
    path = "player/%s/%s" % (pid, slug) if pid else "player/1/" + slug
    txt = _jina(path)

    out = {"slug": slug, "url": HLTV + path}

    m = re.search(r"Title:\s*(.+)", txt)
    if m:
        out["title"] = m.group(1).strip()
    m = re.search(r"Title:\s*([A-Za-z'\- ]+?)\s*'([^']+)'\s*([A-Za-z'\- ]+)", txt)
    if m:
        out["real"] = ("%s %s" % (m.group(1), m.group(3))).strip()
    m = re.search(r"flags/30x20/([A-Z]{2})\.gif", txt)
    if m:
        out["cc"] = m.group(1)
        out["country"] = CC.get(m.group(1), m.group(1))
    m = re.search(r"hltv\.org/team/(\d+)/([a-z0-9\-]+)\)", txt)
    if m:
        out["team_slug"] = m.group(2)
    m = re.search(r"Age\s+(\d+)\s+years?", txt)
    if m:
        out["age"] = int(m.group(1))
    m = re.search(r"\$([\d,]{3,})", txt)
    if m:
        out["prize"] = m.group(1)
    tops = re.findall(r"Top 20\[#(\d+)\]\([^)]*\)\('(\d{2})\)", txt)
    if tops:
        out["top20"] = [{"rank": int(a), "year": b} for a, b in tops]
    ach = re.findall(r"\*\*(\d+)\*\*\s*x\s*([A-Za-z ]{3,30})", txt)
    if ach:
        out["achievements"] = ["%s x %s" % (b.strip(), a) for a, b in ach][:5]

    # 统计区间 + Rating
    m = re.search(r"statistics\(([^)]{3,60})\)", txt)
    if m:
        out["period"] = m.group(1).strip()
    m = (re.search(r"\*\*Rating[^*]{0,24}\*\*[\s\S]{0,90}?(\d\.\d{2})", txt)
         or re.search(r"Rating\s*\n+\s*(\d\.\d{2})", txt)
         or re.search(r"Rating[^0-9\n]{0,30}(\d\.\d{2})", txt))
    if m:
        out["rating"] = m.group(1)
    subs = re.findall(r"\*\*([A-Z][A-Za-z ]{2,14})\*\*[\s\n]{0,40}\*\*(\d{1,3})\*\*/\s*100", txt)
    if subs:
        out["sub"] = [{"name": a.strip(), "score": int(b)} for a, b in subs][:8]
    imp = re.findall(r"\*\*([A-Z][A-Za-z ]{2,14})\*\*\s*\n\s*\*\*(\d{1,3})\*\*/100", txt)
    if imp and not out.get("sub"):
        out["sub"] = [{"name": a.strip(), "score": int(b)} for a, b in imp][:8]

    if out.get("title") or out.get("rating"):
        _cache_put(cname, out)
    return out


def _player_id(slug: str) -> str:
    """HLTV 选手页 URL 需要数字 id。排名页里带着所有前 30 队的阵容链接，够用。"""
    slug = slug.strip().lower()
    cached = _cache_get("hltv_player_ids.json", 86400 * 30) or {}
    if slug in cached:
        return cached[slug]
    for path in ("ranking/teams", "players/archive/active"):
        try:
            txt = _jina(path, retries=2)
        except Exception:
            continue
        for pid, s in re.findall(r"hltv\.org/player/(\d+)/([a-z0-9\-]+)", txt):
            cached.setdefault(s, pid)
        if cached:
            _cache_put("hltv_player_ids.json", cached)
        if slug in cached:
            return cached[slug]
    return cached.get(slug, "")
