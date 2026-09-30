"""把赛事 / 新闻整理成可推送的消息。"""
from __future__ import annotations

import html as _html
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
