"""把赛事 / 新闻整理成可推送的消息。"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def now_local(cfg) -> datetime:
    tz = ZoneInfo(cfg.tz_name) if ZoneInfo else timezone(timedelta(hours=8))
    return datetime.now(tz)


def parse_dt(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_time(cfg, iso: str) -> str:
    dt = parse_dt(iso)
    if not dt:
        return iso or "时间待定"
    tz = ZoneInfo(cfg.tz_name) if ZoneInfo else timezone(timedelta(hours=8))
    d = dt.astimezone(tz)
    return "%d月%d日 %s %02d:%02d" % (d.month, d.day, WEEK[d.weekday()], d.hour, d.minute)


def humanize_hours(h) -> str:
    """把小时数变成人话：2.5 -> 「2小时30分」。"""
    if h is None:
        return ""
    if h < 0:
        return "已开始"
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    if hh >= 24:
        d, rest = hh // 24, hh % 24
        return ("%d天%d小时" % (d, rest)) if rest else ("%d天" % d)
    if hh:
        return ("%d小时%d分" % (hh, mm)) if mm else ("%d小时" % hh)
    return "%d分钟" % mm


def hours_until(iso: str, cfg) -> float | None:
    dt = parse_dt(iso)
    if not dt:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def team_hit(cfg, m: dict) -> list:
    """这场比赛涉及哪些关注队伍。"""
    text = " ".join([m.get("team_a", ""), m.get("team_b", ""), m.get("name", "")])
    return cfg.find_teams(text)


def match_line(cfg, m: dict, with_tier: bool = True) -> str:
    a, b = m.get("team_a") or "?", m.get("team_b") or "?"
    names = []
    for side in (a, b):
        t = None
        for tt in cfg.teams:
            if tt.matches(side):
                t = tt
                break
        names.append(t.label if t else side)
    vs = " vs ".join(names)

    bits = []
    if m.get("tournament"):
        bits.append(m["tournament"])
    if with_tier and m.get("tier_cn"):
        bits.append(m["tier_cn"])
    if m.get("bo"):
        bits.append("BO%s" % m["bo"])
    loc = m.get("location") or m.get("region")
    if loc:
        bits.append("地点：" + loc)
    tail = " · ".join(bits)
    return "%s\n    %s" % (vs, tail) if tail else vs


def build(cfg, matches: list, news: list, schedule_by_team: dict, notes: list) -> tuple:
    """返回 (标题, markdown, html)。"""
    tz_now = now_local(cfg)
    title = "CS2 赛事提醒 · %d月%d日" % (tz_now.month, tz_now.day)

    md = []
    md.append("## %s" % title)

    if matches:
        md.append("")
        md.append("### 即将开始")
        for m in matches:
            h = hours_until(m.get("begin_at", ""), cfg)
            when = ("%d 小时后" % round(h)) if h is not None and h >= 0 else "进行中/即将"
            md.append("- **%s** · %s" % (when, fmt_time(cfg, m.get("begin_at", ""))))
            md.append("    " + match_line(cfg, m).replace("\n", "\n    "))
            if m.get("streams"):
                md.append("    直播：%s" % m["streams"][0])
    else:
        md.append("")
        md.append("### 近期没有已确认的关注战队比赛")

    if schedule_by_team:
        md.append("")
        md.append("### 关注队伍近期赛程")
        for label, rows in schedule_by_team.items():
            md.append("**%s**" % label)
            if not rows:
                md.append("- 暂无安排")
                continue
            for r in rows:
                md.append("- %s  %s" % (fmt_time(cfg, r.get("begin_at", "")),
                                        match_line(cfg, r, with_tier=False).replace("\n", " ")))

    if news:
        md.append("")
        md.append("### 相关新闻")
        for n in news:
            src = n.get("source", "")
            md.append("- [%s] [%s](%s)" % (src, n.get("title", ""), n.get("url", "")))

    if notes:
        md.append("")
        md.append("### 状态")
        for n in notes:
            md.append("- %s" % n)

    markdown = "\n".join(md)

    # HTML 版本（PushPlus / WxPusher / 邮件用）
    body = []
    body.append("<h2>%s</h2>" % _html.escape(title))
    if matches:
        body.append("<h3>即将开始</h3><ul>")
        for m in matches:
            h = hours_until(m.get("begin_at", ""), cfg)
            when = ("%d 小时后" % round(h)) if h is not None and h >= 0 else "进行中/即将"
            body.append("<li><b>%s</b> · %s<br>%s</li>" %
                        (_html.escape(when), _html.escape(fmt_time(cfg, m.get("begin_at", ""))),
                         _html.escape(match_line(cfg, m)).replace("\n", "<br>")))
        body.append("</ul>")
    else:
        body.append("<h3>近期没有已确认的关注战队比赛</h3>")
    if schedule_by_team:
        body.append("<h3>关注队伍近期赛程</h3>")
        for label, rows in schedule_by_team.items():
            body.append("<p><b>%s</b></p><ul>" % _html.escape(label))
            if not rows:
                body.append("<li>暂无安排</li>")
            for r in rows:
                body.append("<li>%s &nbsp; %s</li>" %
                            (_html.escape(fmt_time(cfg, r.get("begin_at", ""))),
                             _html.escape(match_line(cfg, r, with_tier=False)).replace("\n", " ")))
            body.append("</ul>")
    if news:
        body.append("<h3>相关新闻</h3><ul>")
        for n in news:
            body.append('<li>[%s] <a href="%s">%s</a></li>' %
                        (_html.escape(n.get("source", "")), _html.escape(n.get("url", "")),
                         _html.escape(n.get("title", ""))))
        body.append("</ul>")
    if notes:
        body.append("<h3>状态</h3><ul>")
        for n in notes:
            body.append("<li>%s</li>" % _html.escape(n))
        body.append("</ul>")
    html = "<div style='font-family:system-ui,sans-serif;line-height:1.6'>" + "".join(body) + "</div>"

    return title, markdown, html


def build_day_report(cfg, matches, headline=None) -> str:
    """生成「当天赛程」纯文本报告（QQ 不渲染 Markdown，所以用纯文本）。"""
    tz = ZoneInfo(cfg.tz_name) if ZoneInfo else timezone(timedelta(hours=8))
    today = datetime.now(tz).date()

    todays, upcoming = [], []
    for m in matches:
        dt = parse_dt(m.get("begin_at", ""))
        if not dt:
            continue
        d = dt.astimezone(tz)
        hits = team_hit(cfg, m)
        if not hits:
            continue
        (todays if d.date() == today else upcoming).append((d, m, hits))

    todays.sort(key=lambda x: x[0])
    upcoming.sort(key=lambda x: x[0])

    lines = []
    lines.append(headline or ("📺 CS2 赛程 · %d月%d日" % (today.month, today.day)))
    lines.append("")

    if todays:
        for d, m, hits in todays:
            for t in hits:
                lines.append("【%s】" % t.label)
            lines.append("  %02d:%02d  %s vs %s" % (
                d.hour, d.minute,
                _label(cfg, m.get("team_a")), _label(cfg, m.get("team_b"))))
            bits = []
            if m.get("tournament"):
                bits.append(m["tournament"])
            if m.get("tier_cn"):
                bits.append(m["tier_cn"])
            if m.get("bo"):
                bits.append("BO%s" % m["bo"])
            if m.get("location"):
                bits.append("地点:" + m["location"])
            if m.get("stage"):
                bits.append(m["stage"])
            if bits:
                lines.append("        " + " · ".join(bits))
            lines.append("")
    else:
        lines.append("今天没有关注战队的比赛 😴")
        lines.append("")
        if upcoming:
            lines.append("最近的比赛：")
            for d, m, hits in upcoming[:5]:
                lines.append("  %d月%d日 %02d:%02d  %s vs %s" % (
                    d.month, d.day, d.hour, d.minute,
                    _label(cfg, m.get("team_a")), _label(cfg, m.get("team_b"))))
                if m.get("tournament"):
                    lines.append("        %s%s" % (
                        m["tournament"], (" · " + m["tier_cn"]) if m.get("tier_cn") else ""))
        else:
            lines.append("近期也没有已排期的比赛。")

    lines.append("")
    lines.append("— 由赛事提醒机器人自动回复")
    return "\n".join(lines)


def _label(cfg, name: str) -> str:
    """队名换成「中文绰号 英文名」。"""
    if not name:
        return "?"
    for t in cfg.teams:
        if t.matches(name):
            return t.label
    return name


COUNTRY_CN = {
    "china": "中国", "russia": "俄罗斯", "ukraine": "乌克兰", "brazil": "巴西",
    "france": "法国", "denmark": "丹麦", "sweden": "瑞典", "poland": "波兰",
    "germany": "德国", "usa": "美国", "united states": "美国", "canada": "加拿大",
    "finland": "芬兰", "norway": "挪威", "latvia": "拉脱维亚", "estonia": "爱沙尼亚",
    "lithuania": "立陶宛", "kazakhstan": "哈萨克斯坦", "mongolia": "蒙古",
    "south korea": "韩国", "japan": "日本", "australia": "澳大利亚",
    "united kingdom": "英国", "england": "英格兰", "spain": "西班牙",
    "portugal": "葡萄牙", "italy": "意大利", "belgium": "比利时",
    "netherlands": "荷兰", "turkey": "土耳其", "israel": "以色列",
    "romania": "罗马尼亚", "serbia": "塞尔维亚", "bulgaria": "保加利亚",
    "hungary": "匈牙利", "czechia": "捷克", "slovakia": "斯洛伐克",
    "argentina": "阿根廷", "chile": "智利", "south africa": "南非",
    "india": "印度", "indonesia": "印度尼西亚", "vietnam": "越南",
    "taiwan": "中国台湾", "hong kong": "中国香港", "singapore": "新加坡",
    "new zealand": "新西兰", "ireland": "爱尔兰", "switzerland": "瑞士",
    "austria": "奥地利", "belarus": "白俄罗斯", "moldova": "摩尔多瓦",
    "north macedonia": "北马其顿", "bosnia and herzegovina": "波黑",
    "montenegro": "黑山", "kosovo": "科索沃", "georgia": "格鲁吉亚",
}


def cn_country(name: str) -> str:
    if not name:
        return "未知"
    k = name.strip().lower()
    return COUNTRY_CN.get(k, name.strip())


def _rank_cn(rank: str) -> str:
    """26th -> 第 26 名"""
    m = re.match(r"(\d+)\s*(?:st|nd|rd|th)", (rank or "").strip(), re.I)
    if m:
        return "第 %s 名" % m.group(1)
    return (rank or "").strip()


def _team_cn(cfg, name: str) -> str:
    for t in cfg.teams:
        if t.matches(name or ""):
            return t.cn or ""
    return ""


def build_team_report(cfg, team: dict, hltv: dict = None) -> str:
    """战队数据报告（纯文本，QQ 不渲染 Markdown）。"""
    if not team:
        return "没查到这支战队的数据 😕"
    name = team.get("name") or "?"
    cn = _team_cn(cfg, name)
    L = []
    L.append("🏆 %s%s · 战队数据" % (name, ("（%s）" % cn) if cn else ""))
    L.append("")

    L.append("【基本信息】")
    L.append("  国家    ：%s" % cn_country(team.get("country")))
    if team.get("rank"):
        body = team.get("ranking_body") or "世界排名"
        body = body.replace("Global Valve Rankings", "V社世界排名")
        pts = ("（%s 分）" % team["points"]) if team.get("points") else ""
        L.append("  V社排名 ：%s %s%s" % (_rank_cn(team["rank"]), body, pts))
    if hltv and hltv.get("rank"):
        L.append("  HLTV排名：第 %s 名（%s 分）" % (hltv["rank"], hltv.get("points", "?")))
    elif hltv is not None and not hltv:
        L.append("  HLTV排名：未进前 30")
    L.append("")

    st = {k: v for k, v in (team.get("stats") or {}).items()
          if v and v.strip() not in ("-", "0", "")}
    if st:
        L.append("【战绩统计】")
        for k, v in st.items():
            label = {"Matches": "比赛场次", "Matches W/L": "比赛胜负",
                     "Maps": "地图总数", "Maps W/L": "地图胜负"}.get(k, k)
            L.append("  %-8s：%s" % (label, v))
        L.append("")

    roster = team.get("roster") or []
    if roster:
        L.append("【当前阵容】")
        for p in roster:
            bits = [cn_country(p.get("country"))]
            if p.get("real"):
                bits.append(p["real"])
            L.append("  %-12s %s" % (p.get("name", "?"), " · ".join(bits)))
        L.append("")

    recent = team.get("recent") or []
    if recent:
        L.append("【近期比赛】")
        for d, score, opp in recent[:6]:
            opp_cn = _team_cn(cfg, opp)
            L.append("  %s  vs %-16s %s" % (d, opp + (("（%s）" % opp_cn) if opp_cn else ""), score))
        L.append("")

    tours = team.get("tournaments") or []
    if tours:
        L.append("【参赛赛事】")
        for t in tours[:6]:
            L.append("  · " + t)
        L.append("")

    L.append("— 数据来源：BLAST.tv 官方")
    return "\n".join(L)


def build_player_report(cfg, player: dict, team: dict = None, hltv: dict = None) -> str:
    """选手数据报告。"""
    if not player:
        return ("没查到这位选手 😕\n\n"
                "试试用比赛里的 ID（比如 ZywOo、donk、Jee），\n"
                "或者先查战队数据看看阵容。")
    L = []
    L.append("👤 %s · 选手数据" % player.get("name", "?"))
    L.append("")

    L.append("【基本信息】")
    if player.get("real"):
        L.append("  真名    ：%s" % player["real"])
    L.append("  国籍    ：%s" % cn_country(player.get("country")))
    tname = player.get("team") or "?"
    tcn = _team_cn(cfg, tname)
    extra = ""
    if player.get("team_rank"):
        extra = " · 世界%s" % _rank_cn(player["team_rank"])
    L.append("  所属战队：%s%s%s" % (tname, ("（%s）" % tcn) if tcn else "", extra))
    if hltv:
        if hltv.get("team_slug"):
            ts = hltv["team_slug"]
            if ts and ts.lower() not in (tname or "").lower():
                pass
        if hltv.get("age"):
            L.append("  年龄    ：%s 岁" % hltv["age"])
        if hltv.get("prize"):
            L.append("  生涯奖金：$%s" % hltv["prize"])
        if hltv.get("top20"):
            L.append("  Top20   ：" + "、".join(
                "第 %d 名（20%s）" % (x["rank"], x["year"]) for x in hltv["top20"]))
        if hltv.get("achievements"):
            L.append("  荣誉    ：" + "、".join(hltv["achievements"]))
    L.append("")

    if hltv and (hltv.get("rating") or hltv.get("sub")):
        L.append("【HLTV 数据】%s" % (("（%s）" % hltv["period"]) if hltv.get("period") else ""))
        if hltv.get("rating"):
            L.append("  Rating    ：%s" % hltv["rating"])
        for s in (hltv.get("sub") or []):
            L.append("  %-10s：%s / 100" % (s["name"], s["score"]))
        L.append("")

    team = team or {}
    roster = [p for p in (team.get("roster") or [])
              if p.get("slug") != player.get("slug")]
    if roster:
        L.append("【队友】")
        for p in roster:
            bits = [cn_country(p.get("country"))]
            if p.get("real"):
                bits.append(p["real"])
            L.append("  %-12s %s" % (p.get("name", "?"), " · ".join(bits)))
        L.append("")

    recent = (team.get("recent") or [])[:4]
    if recent:
        L.append("【战队近期比赛】")
        for d, score, opp in recent:
            L.append("  %s  vs %-16s %s" % (d, opp, score))
        L.append("")

    L.append("— 数据来源：BLAST.tv 官方")
    return "\n".join(L)


# ============================================================ 智能回复


def build_ranking_report(cfg, rows, top: int = 10) -> str:
    """HLTV 世界排名。"""
    if not rows:
        return "暂时取不到世界排名 😕（HLTV 抓取失败，稍后再试）"
    rows = sorted(rows, key=lambda x: x.get("rank", 999))
    L = ["🏆 HLTV 世界排名（前 %d）" % min(top, len(rows)), ""]
    for r in rows[:top]:
        cn = _team_cn(cfg, r.get("name", ""))
        name = r.get("name", "?")
        if cn:
            name = "%s（%s）" % (name, cn)
        L.append("  #%-3s %-22s %s 分" % (r.get("rank", "?"), name, r.get("points", "?")))
    L.append("")

    # 关注队伍排到多少
    mine = []
    for t in cfg.teams:
        for r in rows:
            if t.matches(r.get("name", "")):
                mine.append("%s #%s" % (t.label, r.get("rank")))
                break
    if mine:
        L.append("关注队伍：" + " · ".join(mine))
        L.append("")
    L.append("— 数据来源：HLTV")
    return "\n".join(L)


def build_compare_report(cfg, items) -> str:
    """两个选手并排对比。items 是 [{player, hltv}, ...]"""
    items = [x for x in items if x and x.get("hltv")]
    if len(items) < 2:
        return ("需要两个能查到数据的选手，比如：\n\n"
                "  对比 donk ZywOo\n"
                "  对比 sh1ro m0NESY")
    a, b = items[0], items[1]
    pa, pb = a["player"], b["player"]
    ha, hb = a["hltv"], b["hltv"]

    L = ["⚖️ 选手对比", ""]
    L.append("  %-12s %-18s %-18s" % ("", pa.get("name", "?"), pb.get("name", "?")))
    L.append("  " + "-" * 50)

    def row(label, va, vb):
        L.append("  %-12s %-18s %-18s" % (label, str(va)[:17], str(vb)[:17]))

    row("战队", pa.get("team", "?"), pb.get("team", "?"))
    row("国籍", cn_country(pa.get("country")), cn_country(pb.get("country")))
    row("真名", pa.get("real") or "-", pb.get("real") or "-")
    row("年龄", ("%s 岁" % ha["age"]) if ha.get("age") else "-",
        ("%s 岁" % hb["age"]) if hb.get("age") else "-")
    row("生涯奖金", ("$" + ha["prize"]) if ha.get("prize") else "-",
        ("$" + hb["prize"]) if hb.get("prize") else "-")
    if ha.get("top20") or hb.get("top20"):
        fmt = lambda h: "、".join("第%d(%s)" % (x["rank"], x["year"]) for x in (h.get("top20") or [])) or "-"
        row("Top20", fmt(ha), fmt(hb))

    L.append("  " + "-" * 50)
    row("Rating", ha.get("rating") or "-", hb.get("rating") or "-")

    sa = {x["name"]: x["score"] for x in (ha.get("sub") or [])}
    sb = {x["name"]: x["score"] for x in (hb.get("sub") or [])}
    for k in ("Firepower", "Opening", "Clutching", "Sniping", "Utility", "Entrying", "Trading"):
        if k in sa or k in sb:
            va, vb = sa.get(k), sb.get(k)
            mark_a = " ★" if (va is not None and vb is not None and va > vb) else ""
            mark_b = " ★" if (va is not None and vb is not None and vb > va) else ""
            row(k, ("%s%s" % (va, mark_a)) if va is not None else "-",
                ("%s%s" % (vb, mark_b)) if vb is not None else "-")

    L.append("")
    L.append("★ = 该项更强")
    L.append("— 数据来源：BLAST.tv + HLTV")
    return "\n".join(L)


def build_result_report(cfg, results, title=None) -> str:
    """赛后战报。results: [{date, team, opponent, score, win, tournament}]"""
    if not results:
        return ""
    L = [title or "📢 赛后战报", ""]
    for r in results:
        tag = "✅ 胜" if r.get("win") is True else ("❌ 负" if r.get("win") is False else "➖ 平/未定")
        L.append("%s  %s" % (tag, r.get("headline", "")))
        for extra in r.get("lines", []):
            L.append("    " + extra)
        L.append("")
    L.append("— 数据来源：BLAST.tv 官方")
    return "\n".join(L)


def build_record_line(results, team_label: str) -> str:
    """近 N 场战绩一行。results 已按时间倒序。"""
    if not results:
        return ""
    w = sum(1 for r in results if r.get("win") is True)
    l = sum(1 for r in results if r.get("win") is False)
    return "%s：近 %d 场 %d 胜 %d 负" % (team_label, len(results), w, l)


HELP_TEXT = """🤖 CS2 赛事助手 · 我能做什么

【查赛程】
  赛事推送 / 赛程 / 今天比赛
  天禄下一场打谁
  绿龙什么时候比赛

【查战队】
  天禄 / 绿龙 / 小蜜蜂 / TYLOO / Vitality
  查询 天禄

【查选手】
  donk / ZywOo / Jee / sh1ro
  查询 donk

【排行榜】
  世界排名 / 前十 / 排行榜

【对比】
  对比 donk ZywOo

【战绩】
  绿龙最近赢了吗 / 天禄战绩

【追问】
  问过一次后可以直接说「那他们下一场呢」

—— 打错字也能认：tlyoo、tianlu、DONK
"""
