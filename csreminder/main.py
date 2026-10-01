"""主流程：拉取 -> 过滤关注战队 -> 组装 -> 推送。"""
from __future__ import annotations

import argparse
import json
import re
import time
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from . import config as cfgmod
from . import format as fmtmod
from . import notify
from . import state as statemod
from .sources import blasttv, fiveplay, pandascore, steamnews

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log(cfg, msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        rel = cfg.output.get("log_file", "logs/cs-reminder.log")
        path = rel if os.path.isabs(rel) else os.path.join(BASE_DIR, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def collect(cfg):
    notes = []

    # 赛程数据：BLAST.tv 优先（免注册），PandaScore 作为补充
    ps = blasttv.fetch_all(cfg)
    if ps["ok"]:
        extra = pandascore.fetch_all(cfg)
        if extra["ok"] and extra["upcoming"]:
            known = set()
            for x in ps["upcoming"]:
                known.add((x.get("team_a", "").lower(), x.get("team_b", "").lower()))
            for x in extra["upcoming"]:
                key = (x.get("team_a", "").lower(), x.get("team_b", "").lower())
                rkey = (key[1], key[0])
                if key not in known and rkey not in known:
                    ps["upcoming"].append(x)
                    known.add(key)
    else:
        notes.append("BLAST.tv 赛程不可用：" + ps["error"])
        alt = pandascore.fetch_all(cfg)
        if alt["ok"]:
            ps = alt
            notes.append("已改用 PandaScore 赛程")
        elif not (alt.get("error") or "").startswith("未配置"):
            notes.append("PandaScore 也不可用：" + alt["error"])

    news = []
    if cfg.rules.get("include_news", True):
        fp = fiveplay.fetch_news(cfg)
        if fp["ok"]:
            news += fp["items"]
        else:
            notes.append(fp["error"])
        st = steamnews.fetch_news(cfg)
        if st["ok"]:
            news += st["items"]
        else:
            notes.append(st["error"])
    return ps, news, notes


def relevant(cfg, matches):
    return [m for m in matches if fmtmod.team_hit(cfg, m)]


def in_quiet_hours(cfg) -> bool:
    """当前是否落在静默时段（本地时区）。支持跨零点，如 [23, 7]。"""
    qh = cfg.rules.get("quiet_hours") or []
    if len(qh) != 2:
        return False
    try:
        start, end = int(qh[0]), int(qh[1])
    except (TypeError, ValueError):
        return False
    h = fmtmod.now_local(cfg).hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end          # 跨零点


def pick_due(cfg, matches, st, force):
    """按 remind_hours_before 决定这次要提醒哪些比赛。"""
    thresholds = sorted(cfg.rules.get("remind_hours_before", [24, 2]))
    due, seen = [], set()
    for m in matches:
        mid = m.get("id")
        if mid in seen:
            continue
        h = fmtmod.hours_until(m.get("begin_at", ""), cfg)
        if h is None or h < 0:
            continue
        for th in thresholds:
            if h <= th:
                key = "match:%s:%sh" % (mid, th)
                if force or not st.already_sent(key):
                    due.append((m, th, key))
                seen.add(mid)
                break
    return due


def build_team_schedule(cfg, upcoming):
    limit_hours = cfg.sources.get("pandascore", {}).get("lookahead_days", 14) * 24
    out = {}
    for t in cfg.teams:
        rows = []
        for m in upcoming:
            if not t.matches(" ".join([m.get("team_a", ""), m.get("team_b", ""), m.get("name", "")])):
                continue
            h = fmtmod.hours_until(m.get("begin_at", ""), cfg)
            if h is None or h < -2 or h > limit_hours:
                continue
            rows.append(m)
        rows.sort(key=lambda x: x.get("begin_at") or "")
        out[t.label] = rows[:6]
    return out


def _schedule_only(cfg):
    """只拉赛程（给指令回复用，跳过新闻，快很多）。"""
    ps = blasttv.fetch_all(cfg)
    if ps["ok"]:
        return ps
    alt = pandascore.fetch_all(cfg)
    return alt if alt["ok"] else ps


def _extract_name(text: str, keys) -> str:
    """从「查询战队数据 天禄」「查询天禄」里抠出「天禄」。"""
    s = text or ""
    for k in sorted(set(keys), key=len, reverse=True):
        s = s.replace(k, " ")
    return re.sub(r"[\s,，。！!？?@#:：\-]+", "", s).strip()


# 自动判断：先当战队找（快），找不到再当选手找
def _auto_cmd(cfg, name: str) -> str:
    from .sources import blastteams
    if not name:
        return ("要查什么？这样发：\n\n"
                "  查询 donk      → 选手数据\n"
                "  查询 天禄       → 战队数据\n"
                "  查询 ZywOo     → 选手数据")
    # ① 先精确当战队找
    try:
        r = blastteams.find_team(cfg, name, strict=True)
    except TypeError:
        r = blastteams.find_team(cfg, name)
    if r:
        return _team_cmd(cfg, name)
    # ② 再当选手找
    try:
        p = blastteams.find_player(cfg, name)
    except Exception:
        p = None
    if p:
        return _player_cmd(cfg, name)
    return ("没查到「%s」😕\n\n"
            "· 战队试试：天禄 / 小蜜蜂 / 绿龙 / TYLOO / Vitality / Spirit\n"
            "· 选手试试：donk / ZywOo / Jee / sh1ro" % name)


def _day_report(cfg) -> str:
    """当天赛程。"""
    ps = _schedule_only(cfg)
    if not ps.get("ok"):
        return "抱歉，赛程数据暂时取不到：\n%s" % (ps.get("error") or "未知错误")[:200]
    matches = list(ps.get("upcoming") or []) + list(ps.get("past") or [])
    return fmtmod.build_day_report(cfg, matches)


def _match_involves(cfg, name: str, match: dict) -> bool:
    """这场比赛是否和 name 有关（队名/别名/中文绰号）。"""
    from .sources import blastteams as _bt
    r = None
    try:
        r = _bt.find_team(cfg, name, strict=True)
    except TypeError:
        r = _bt.find_team(cfg, name)
    target = (r["slug"] if r else name).lower().replace("-", "")
    blob = ((match.get("team_a") or "") + " " + (match.get("team_b") or "") +
            " " + (match.get("teams") and " ".join(match["teams"]) or "")).lower()
    if target and target in blob.replace("-", "").replace(" ", ""):
        return True
    for t in cfg.teams:
        if t.matches(name) and t.matches(blob):
            return True
    return False


def _next_matches(cfg, name: str) -> str:
    """某人/某队的下一场比赛。"""
    ps = _schedule_only(cfg)
    if not ps.get("ok"):
        return "赛程暂时取不到：%s" % (ps.get("error") or "未知错误")[:120]
    ups = [x for x in (ps.get("upcoming") or []) if _match_involves(cfg, name, x)]
    ups.sort(key=lambda x: x.get("begin_at") or "")
    if not ups:
        return "「%s」近期没有已排期的比赛 😴" % name
    L = ["📅 %s 的下一场比赛" % name, ""]
    for x in ups[:3]:
        h = fmtmod.hours_until(x.get("begin_at", ""), cfg)
        tz = ZoneInfo(cfg.tz_name) if ZoneInfo else None
        dt = fmtmod.parse_dt(x.get("begin_at", ""))
        when = ""
        if dt:
            d = dt.astimezone(tz) if tz else dt
            when = "%d月%d日 %02d:%02d" % (d.month, d.day, d.hour, d.minute)
        L.append("  %s%s" % (when, ("（%s后）" % fmtmod.humanize_hours(h)) if h is not None else ""))
        L.append("    %s vs %s" % (x.get("team_a", "?"), x.get("team_b", "?")))
        bits = [b for b in [x.get("tournament"), x.get("tier_cn"),
                            ("BO%s" % x["bo"]) if x.get("bo") else "", x.get("location")] if b]
        if bits:
            L.append("    " + " · ".join(bits))
        L.append("")
    L.append("— 数据来源：BLAST.tv 官方")
    return "\n".join(L)


def _recent_of_team(cfg, name: str):
    """抓某队最近比赛（含比分）。返回 (team_dict, [(date, score, opp, win)])"""
    from .sources import blastteams
    r = None
    try:
        r = blastteams.find_team(cfg, name, strict=True)
    except TypeError:
        r = blastteams.find_team(cfg, name)
    if not r:
        return {}, []
    team = blastteams.fetch_team("%s/%s" % (r["id"], r["slug"]))
    out = []
    for item in (team.get("recent") or []):
        try:
            d, score, opp = item[0], item[1], item[2]
        except Exception:
            continue
        mm = re.match(r"\s*(\d+)\s*:\s*(\d+)", score or "")
        win = None
        if mm:
            a, b = int(mm.group(1)), int(mm.group(2))
            if a or b:
                win = a > b
        out.append({"date": d, "score": score, "opp": opp, "win": win,
                    "finished": d < _today(cfg)})
    return team, out


def _today(cfg) -> str:
    tz = ZoneInfo(cfg.tz_name) if ZoneInfo else None
    return (datetime.now(tz) if tz else datetime.now()).strftime("%Y-%m-%d")


def _results_for(cfg, name: str) -> str:
    """最近战绩。"""
    team, rl = _recent_of_team(cfg, name)
    if not rl:
        return "没查到「%s」的比赛记录 😕" % name
    done = [x for x in rl if x["finished"]]
    L = ["📊 %s 近期战绩" % (team.get("name") or name), ""]
    if done:
        w = sum(1 for x in done if x["win"] is True)
        l = sum(1 for x in done if x["win"] is False)
        L.append("  近 %d 场：%d 胜 %d 负" % (len(done), w, l))
        L.append("")
        for x in done[:6]:
            tag = "✅" if x["win"] is True else ("❌" if x["win"] is False else "➖")
            L.append("  %s %s  %-8s vs %s" % (tag, x["date"], x["score"], x["opp"]))
    else:
        L.append("  还没有已结束的比赛。")
    up = [x for x in rl if not x["finished"]]
    if up:
        L.append("")
        L.append("  待赛：")
        for x in up[:3]:
            L.append("     %s  vs %s" % (x["date"], x["opp"]))
    L.append("")
    L.append("— 数据来源：BLAST.tv 官方")
    return "\n".join(L)


def _smart_ask(cfg, text: str, target: str = "", is_group: bool = False) -> str:
    """一句话 -> 回复。监听器和 --ask 共用这一套。"""
    from . import context as ctxmod
    from . import nlu

    text = (text or "").strip()
    if not text:
        return ""

    last = ctxmod.get(target) if target else {}
    pi = None
    try:
        from .sources import blastteams
        pi = blastteams.load_player_index(cfg)
    except Exception:
        pi = None

    r = nlu.describe(text, cfg, pi, last)
    intent, kind, name = r["intent"], r["kind"], r["name"]

    if name and kind and target:
        ctxmod.put(target, kind, name, intent)

    # ---- 帮助 ----
    if intent == "help":
        return fmtmod.HELP_TEXT

    # ---- 排行榜 ----
    if intent == "ranking":
        try:
            from .sources import hltv
            return fmtmod.build_ranking_report(cfg, hltv.ranking())
        except Exception as e:
            return "排行榜抓取失败：%s" % str(e)[:120]

    # ---- 对比 ----
    if intent == "compare":
        picks = []
        for cand in (pi or {}).values():
            pass
        # 从文本里找出两个名字
        words = [w for w in re.split(r"[\s,，。!！?？/]+", text) if w]
        found = []
        for w in words:
            k2, n2, _ = nlu.match_entity(w, cfg, pi)
            if n2 and n2 not in [x[1] for x in found]:
                found.append((k2, n2))
        if name and name not in [x[1] for x in found]:
            found.insert(0, (kind, name))
        for k2, n2 in found[:2]:
            try:
                from .sources import blastteams, hltv
                rr = None
                try:
                    rr = blastteams.find_team(cfg, n2, strict=True)
                except TypeError:
                    rr = blastteams.find_team(cfg, n2)
                if rr:
                    continue
                pr = blastteams.find_player(cfg, n2)
                if pr:
                    picks.append({"player": pr, "hltv": hltv.player_stats(pr.get("slug") or n2)})
            except Exception:
                continue
        return fmtmod.build_compare_report(cfg, picks)

    # ---- 有实体 ----
    if name:
        if intent == "schedule":
            return _next_matches(cfg, name)
        if intent == "result":
            return _results_for(cfg, name)
        if kind == "player":
            return _player_cmd(cfg, name)
        if kind == "team":
            return _team_cmd(cfg, name)
        return _auto_cmd(cfg, name)

    # ---- 没实体 ----
    # 先看是不是 config 里定义的赛程触发词（赛事推送 / 赛程 / 今日赛程 / 今天比赛）
    cmds = cfg.raw.get("commands") or {}
    triggers = [t for t in (cmds.get("triggers") or ["赛事推送", "赛程"]) if t]
    norm_txt = nlu.normalize(text)
    for t in sorted(triggers, key=len, reverse=True):
        nt = nlu.normalize(t)
        if nt and nt in norm_txt:
            return _day_report(cfg)

    if intent == "schedule":
        return _day_report(cfg)
    if intent == "result":
        L = ["📊 关注队伍近期战绩", ""]
        for t in cfg.teams:
            _tm, rl = _recent_of_team(cfg, t.name)
            done = [x for x in rl if x["finished"]]
            if done:
                L.append("  " + fmtmod.build_record_line(done, t.label))
        L.append("")
        L.append("— 数据来源：BLAST.tv 官方")
        return "\n".join(L)

    # ---- NLU 没认出实体？直接用查找兜底（BLAST 严格 -> BLAST 宽松 -> HLTV 679 人大字典 -> 战队）----
    if not name and not intent:
        from .sources import blastteams as _bt
        probe = (text or "").strip()
        if 1 <= len(probe) <= 20 and " " not in probe:
            found = None
            for fn in (lambda: _bt.find_player(cfg, probe, strict=True),
                       lambda: _bt.find_player(cfg, probe)):
                try:
                    found = fn()
                except Exception:
                    found = None
                if found:
                    break
            if found:
                kind, name = "player", found["name"]
            else:
                try:
                    from .sources import hltv as _hl
                    hp = _hl.find_player(probe, online=True)
                    if hp:
                        kind, name = "player", hp.get("name") or hp.get("slug")
                except Exception:
                    pass
            if not name:
                try:
                    if _bt.find_team(cfg, probe, strict=True):
                        kind, name = "team", probe
                except Exception:
                    pass
            if name:
                if target:
                    ctxmod.put(target, kind, name,
                               "player" if kind == "player" else "team")
                # 找到了就直接答，别再往下掉到「没看懂」
                if kind == "player":
                    return _player_cmd(cfg, name)
                return _team_cmd(cfg, name)

    # ---- 兜底：不认识的也给个提示，别让人以为机器人坏了 ----
    head = (text or "").strip()[:16]
    return ("没看懂「%s」😅\n\n"
            "你可以这样说：\n"
            "  donk / 天禄 / 绿龙      查战队或选手\n"
            "  世界排名                 HLTV 世界前 10\n"
            "  对比 donk ZywOo          两个选手对比\n"
            "  天禄下一场打谁            下一场比赛\n"
            "  绿龙最近赢了吗            近期战绩\n"
            "  赛事推送                 当天赛程\n"
            "  帮助                     完整说明" % head)


def build_players_cmd(cfg, args) -> int:
    """把 HLTV 所有能拿的选手列表合并成一个大字典。"""
    import json as _json
    import time as _t
    from .sources import hltv

    limit = int(args.build_players) or 40
    players = hltv.all_players()
    _log(cfg, "已有 %d 人，继续补充" % len(players))

    # 数据源：活跃 / 退役 / 各月选手排名
    sources = ["players/archive/active", "players/archive/retired", "players/top20"]
    # Top20 历年（含已退役传奇：f0rest / olofmeister / shox / flusha ...）
    for y in range(2013, 2027):
        sources.append("players/top20?year=%d" % y)
    for ym in ("2026/september", "2026/august", "2026/july", "2026/june",
               "2025/december", "2025/june", "2024/december"):
        sources.append("ranking/players/" + ym)

    t0 = _t.time()
    for src in sources:
        maxp = limit if "archive" in src else 1
        for page in range(1, maxp + 1):
            path = src + (("?page=%d" % page) if page > 1 else "")
            try:
                txt = hltv._jina(path, retries=2)
            except Exception as e:
                _log(cfg, "  %-28s 第%d页 失败：%s" % (src, page, str(e)[:60]))
                break
            pairs = re.findall(r"hltv\.org/player/(\d+)/([a-z0-9\-]+)", txt)
            if not pairs:
                break
            before = len(players)
            for pid, slug in pairs:
                players.setdefault(slug, {"id": pid, "slug": slug, "name": slug})
            got = len(players) - before
            _log(cfg, "  %-28s 第%2d页 +%-4d 累计 %-5d (%.0fs)"
                 % (src.split("/")[-1], page, got, len(players), _t.time() - t0))
            if not pairs:
                break
            _t.sleep(1)
    # 再从项目里已有的 BLAST 选手索引补一批
    try:
        from .sources import blastteams
        pi = blastteams.load_player_index(cfg)
        for k, v in pi.items():
            players.setdefault(v.get("slug") or k, {"id": "", "slug": v.get("slug") or k, "name": v.get("name") or k})
        _log(cfg, "  合并 BLAST 选手索引，累计 %d" % len(players))
    except Exception as e:
        _log(cfg, "  BLAST 合并失败：%s" % str(e)[:60])

    hltv.save_all_players(players)
    _log(cfg, "完成：共 %d 名选手，总耗时 %.0f 秒" % (len(players), _t.time() - t0))
    return 0


def warm_cmd(cfg, args) -> int:
    """预热所有缓存，让后续查询秒回。"""
    import time as _t
    t0 = _t.time()
    from .sources import blastteams

    _log(cfg, "预热开始…")

    # 1) 战队索引
    try:
        idx = blastteams.load_team_index()
        _log(cfg, "  战队索引：%d 支（%.1fs）" % (len(idx), _t.time() - t0))
    except Exception as e:
        _log(cfg, "  战队索引失败：%s" % str(e)[:80])

    # 2) 选手索引（最慢的一步）
    t1 = _t.time()
    try:
        pi = blastteams.load_player_index(cfg)
        _log(cfg, "  选手索引：%d 人（%.1fs）" % (len(pi), _t.time() - t1))
    except Exception as e:
        _log(cfg, "  选手索引失败：%s" % str(e)[:80])

    # 3) 赛程表（并行抓 20 个赛事页）
    t3 = _t.time()
    try:
        ps = blasttv.fetch_all(cfg)
        _log(cfg, "  赛程：%d 场（%.1fs）" % (len(ps.get("upcoming") or []), _t.time() - t3))
    except Exception as e:
        _log(cfg, "  赛程预热失败：%s" % str(e)[:80])

    # 4) HLTV 世界排名 + 选手 ID 表
    t2 = _t.time()
    try:
        from .sources import hltv
        rows = hltv.ranking()
        _log(cfg, "  HLTV 排名：%d 支（%.1fs）" % (len(rows), _t.time() - t2))
    except Exception as e:
        _log(cfg, "  HLTV 排名失败：%s" % str(e)[:100])

    # 5) 关注队伍的 HLTV 选手数据（这样交互时直接命中缓存）
    try:
        from .sources import hltv
        for t in cfg.teams:
            r = hltv.team_rank(t.name)
            for slug in (r.get("roster") or [])[:5]:
                try:
                    hltv.player_stats(slug)
                except Exception:
                    pass
        _log(cfg, "  关注队伍选手数据已缓存")
    except Exception as e:
        _log(cfg, "  选手数据预热失败：%s" % str(e)[:80])

    _log(cfg, "预热完成，总耗时 %.1f 秒" % (_t.time() - t0))
    return 0


def results_cmd(cfg, args) -> int:
    """赛后战报：关注队伍有新的完赛结果就推送。"""
    st = statemod.State(os.path.join(BASE_DIR, "data", "state.json"))
    fresh = []

    for t in cfg.teams:
        try:
            team, rl = _recent_of_team(cfg, t.name)
        except Exception as e:
            _log(cfg, "  抓 %s 战绩失败：%s" % (t.label, str(e)[:80]))
            continue
        today = _today(cfg)
        same_day_only = cfg.rules.get("results_same_day_only", True)
        for x in rl:
            if not x["finished"] or x["win"] is None:
                continue
            # 只报当天的比赛，不翻旧账
            if same_day_only and x["date"] != today:
                continue
            key = "res:%s:%s:%s" % (t.name, x["date"], x["opp"])
            if st.already_sent(key):
                continue
            fresh.append((key, {
                "date": x["date"],
                "headline": "%s  %s  %s" % (t.label, x["score"], x["opp"]),
                "lines": [x["date"]],
                "win": x["win"],
            }))

    if not fresh:
        _log(cfg, "没有新的比赛结果")
        return 0

    title = "📢 赛后战报 · %s" % _today(cfg)
    md = fmtmod.build_result_report(cfg, [v for _, v in fresh], title)
    html = "<h3>%s</h3><pre style=\"font-family:Consolas,monospace;white-space:pre-wrap\">%s</pre>" % (
        title, md.replace("&", "&amp;").replace("<", "&lt;"))

    if args.dry_run:
        print(md)
        return 0

    res = notify.send_all(cfg, title, md, html)
    ok_any = False
    for name, ok, msg in res:
        _log(cfg, "  推送 %s %s: %s" % ("OK " if ok else "FAIL", name, msg))
        ok_any = ok_any or ok
    if not ok_any:
        _log(cfg, "所有通道都失败，本次不标记已发送")
        return 2

    for k, _ in fresh:
        st.mark_sent(k, {})
    st.save()
    _log(cfg, "已推送 %d 条赛后战报" % len(fresh))
    return 0


def _hltv_rank(cfg, name):
    """拿 HLTV 排名，失败返回 None（不阻塞）。"""
    try:
        from .sources import hltv
        return hltv.team_rank(name)
    except Exception as e:
        print("  [HLTV 排名获取失败] %s: %s" % (type(e).__name__, str(e)[:90]), flush=True)
        return None


def _team_cmd(cfg, name: str) -> str:
    from .sources import blastteams
    if not name:
        return ("要查哪支战队？这样发：\n\n"
                "  查询战队数据 天禄\n"
                "  查询战队数据 绿龙\n"
                "  查询战队数据 Vitality")
    r = blastteams.find_team(cfg, name)
    if not r:
        return ("没找到「%s」这支战队 😕\n\n"
                "试试队名或中文绰号：天禄 / 小蜜蜂 / 绿龙 / TYLOO / Vitality / Spirit" % name)
    # BLAST 战队页 和 HLTV 排名 并行抓，省一半时间
    from concurrent.futures import ThreadPoolExecutor
    guess = r["slug"].replace("-", " ")
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_team = ex.submit(blastteams.fetch_team, "%s/%s" % (r["id"], r["slug"]))
        f_rank = ex.submit(_hltv_rank, cfg, guess)
        try:
            team = f_team.result()
        except Exception as e:
            return "抓取战队数据失败：%s" % str(e)[:120]
        try:
            hr = f_rank.result()
        except Exception:
            hr = None
    # 名字对不上就再用 BLAST 的正式名查一次（排名已缓存，很快）
    if not hr and team.get("name") and team["name"].lower() != guess.lower():
        hr = _hltv_rank(cfg, team["name"])
    return fmtmod.build_team_report(cfg, team, hr)


def _player_cmd(cfg, name: str) -> str:
    from .sources import blastteams
    if not name:
        return ("要查哪位选手？这样发：\n\n"
                "  查询选手数据 Jee\n"
                "  查询选手数据 ZywOo")
    p = None
    try:
        p = blastteams.find_player(cfg, name, strict=True)
    except Exception:
        p = None
    if not p:
        try:
            p = blastteams.find_player(cfg, name)
        except Exception:
            p = None
    # 本地索引没有？用 HLTV 全量选手字典兜底（收录几千人）
    if not p:
        try:
            from .sources import hltv
            hp = hltv.find_player(name)
            if hp:
                p = {"name": hp.get("name") or name, "slug": hp.get("slug") or name,
                     "id": hp.get("id", ""), "real": "", "country": "", "team": "",
                     "team_slug": "", "team_rank": "", "_from_hltv_only": True}
        except Exception:
            p = None
    if not p:
        return ("没查到「%s」这位选手 😕\n\n"
                "试试比赛里的 ID（Jee、ZywOo、donk、sh1ro…），\n"
                "中文队名也行：天禄 / 小蜜蜂 / 绿龙" % name)
    from concurrent.futures import ThreadPoolExecutor
    team, hp = {}, None
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_team = ex.submit(blastteams.fetch_team, p["team_slug"]) if p.get("team_slug") else None
        def _hp():
            from .sources import hltv
            return hltv.player_stats(p.get("slug") or name)
        f_hp = ex.submit(_hp)
        if f_team:
            try:
                team = f_team.result()
            except Exception:
                team = {}
        try:
            hp = f_hp.result()
        except Exception as e:
            print("  [HLTV 选手数据获取失败] %s: %s" % (type(e).__name__, str(e)[:90]), flush=True)
    return fmtmod.build_player_report(cfg, p, team, hp)


def qq_listen_cmd(cfg, args):
    """常驻监听 QQ 消息，响应「赛事推送」等指令。"""
    from . import qqapi, qqlisten

    ch = None
    for c in cfg.notify.get("channels", []):
        if c.get("type") == "qqofficial":
            ch = c
            break
    appid, secret = qqapi.split_credential(str((ch or {}).get("appId") or ""),
                                           str((ch or {}).get("clientSecret") or ""))
    if not appid or not secret:
        print("请先在 config.json 的 qqofficial 通道里填好 appId 和 clientSecret。")
        return 1

    cmds = cfg.raw.get("commands") or {}
    triggers = [t for t in (cmds.get("triggers") or ["赛事推送", "赛程"]) if t]
    team_keys = [t for t in (cmds.get("team_keys") or ["战队数据", "战队"]) if t]
    player_keys = [t for t in (cmds.get("player_keys") or ["选手数据", "选手"]) if t]
    query_keys = [t for t in (cmds.get("query_keys") or ["查询", "查一下", "查"]) if t]

    print("=" * 62)
    print("  指令监听已启动")
    print()
    print("  免前缀直接说名字：「donk」「天禄」「绿龙」")
    print("  自然问句：「天禄下一场打谁」「绿龙最近赢了吗」")
    print("  其他：「世界排名」「对比 donk ZywOo」「帮助」")
    print()
    print("  用法：在 QQ 里私聊机器人，或在群里 @机器人")
    print()
    print("  按 Ctrl+C 停止")
    print("=" * 62)
    print()

    cache = {}

    def _cached(key, fn, ttl=600):
        now = time.time()
        hit = cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        cache[key] = (now, val)
        return val

    def handle(text, kind, target):
        """所有消息都交给智能层处理。"""
        is_group = (kind == "group")
        try:
            reply = _smart_ask(cfg, text, target, is_group)
        except Exception as e:
            print("  处理出错: %s: %s" % (type(e).__name__, str(e)[:150]), flush=True)
            reply = "出了点小问题，稍后再试 \ud83d\ude48"
        return reply or None


    return qqlisten.listen(appid, secret, handle,
                           log=lambda s: print(s, flush=True),
                           max_seconds=args.seconds)

def qq_capture_cmd(cfg, args):
    """连网关抓 openid，抓到后写回 config.json。"""
    from . import qqapi, qqcapture

    ch = None
    for c in cfg.notify.get("channels", []):
        if c.get("type") == "qqofficial":
            ch = c
            break
    appid, secret = qqapi.split_credential(str((ch or {}).get("appId") or ""),
                                           str((ch or {}).get("clientSecret") or ""))
    if not appid or not secret:
        print("请先在 config.json 的 qqofficial 通道里填好 appId 和 clientSecret。")
        return 1

    print("=" * 62)
    print("  请现在去手机 / 电脑 QQ 上做下面任意一件事：")
    print()
    print("  【方式A · 单聊】把机器人「cs赛事推送」加到消息列表，给它发一句话")
    print("      → 之后提醒会直接发到你的 QQ 私聊，最方便")
    print()
    print("  【方式B · 群聊】建一个 QQ 群（你是群主），把机器人拉进群，")
    print("      然后在群里发一条消息并 @机器人")
    print()
    share = ""
    try:
        me = qqapi.api_get("/users/@me", appid, qqapi.get_token(appid, secret))
        share = me.get("share_url") or ""
        print("  机器人名称：%s" % me.get("username"))
    except Exception as e:
        print("  (读机器人信息失败：%s)" % str(e)[:80])
    if share:
        print("  机器人加群/加好友链接：")
        print("      %s" % share)
    print("=" * 62)
    print()

    try:
        found = qqcapture.capture(appid, secret, wait_seconds=args.wait,
                                  log=lambda s: print(s, flush=True))
    except Exception as e:
        print("抓取失败：%s" % e)
        return 1

    groups, users, guilds = found.get("groups", {}), found.get("users", {}), found.get("guilds", {})
    if not (groups or users or guilds):
        print()
        print("没抓到任何消息。可能是：")
        print("  1. 机器人还没被加进群 / 还没加你为好友")
        print("  2. 群里发消息时没有 @机器人（QQ 群必须 @ 才会推给机器人）")
        print("  3. 等待时间太短，重跑时加长：run.bat --qq-capture --wait 300")
        return 1

    print()
    print("抓到了：")
    if users:
        for k in users:
            print("  单聊 user_openid  = %s" % k)
    if groups:
        for k in groups:
            print("  群  group_openid  = %s" % k)
    if guilds:
        for k in guilds:
            print("  频道 channel_id   = %s" % k)

    # 写回配置：单聊优先（最直接），其次群，最后频道
    import shutil
    shutil.copyfile(cfg.path, cfg.path + ".bak")
    for c in cfg.notify["channels"]:
        if c.get("type") == "qqofficial":
            c["enabled"] = True
            c.pop("user_openid", None)
            c.pop("group_openid", None)
            c.pop("channel_id", None)
            if users:
                c["user_openid"] = list(users)[0]
                print("\n→ 已写入 user_openid（提醒将发到你的 QQ 私聊）")
            elif groups:
                c["group_openid"] = list(groups)[0]
                print("\n→ 已写入 group_openid（提醒将发到群里）")
            else:
                c["channel_id"] = list(guilds)[0]
                print("\n→ 已写入 channel_id（提醒将发到频道）")
    with open(cfg.path, "w", encoding="utf-8") as f:
        json.dump(cfg.raw, f, ensure_ascii=False, indent=2)
    print("   配置已保存（旧配置备份在 config.json.bak）")
    print()
    print("下一步：双击  2 发测试消息.bat  验证")
    return 0


def qq_discover(cfg):
    """列出 QQ 机器人所在的频道 / 子频道 ID，方便填 channel_id。"""
    from . import qqapi

    ch = None
    for c in cfg.notify.get("channels", []):
        if c.get("type") == "qqofficial":
            ch = c
            break
    appid, secret = qqapi.split_credential(str((ch or {}).get("appId") or ""),
                                           str((ch or {}).get("clientSecret") or ""))
    if not appid or not secret:
        print("请先在 config.json 的 qqofficial 通道里填好 appId 和 clientSecret，再运行本命令。")
        print("（在 q.qq.com → 点你的机器人 → 左侧「开发设置」里）")
        print("提示：平台给的若是 102123456:AbCdEf... 这种连写形式，")
        print("      把整条粘到 appId 里、clientSecret 留空也可以。")
        return 1

    print("正在查询机器人所在的频道...")
    try:
        res = qqapi.discover(appid, secret)
    except Exception as e:
        print("查询失败：%s" % e)
        print("常见原因：appId/clientSecret 填错；或机器人还没被添加到任何频道。")
        return 1

    guilds = res.get("guilds") or []
    if not guilds:
        print("机器人还没有加入任何频道。")
        print("请先在 QQ 客户端里：进入你自己的频道 → 频道设置 → 添加机器人 → 选择你的机器人。")
        return 1

    for g in guilds:
        print("")
        print("频道组：%s   guild_id=%s" % (g.get("guild_name"), g.get("guild_id")))
        for c in g.get("channels") or []:
            if c.get("error"):
                print("   [!] %s" % c["error"])
                continue
            print("   子频道：%-22s id=%s" % (c.get("name"), c.get("id")))
    print("")
    print("把你要接收提醒的「子频道 id」填进 config.json → notify.channels 里 qqofficial 的 channel_id")
    return 0


def demo_data():
    """演示用赛程（时间相对当前时刻，便于预览格式）。"""
    from datetime import datetime, timedelta, timezone as _tz

    def iso(hours):
        return (datetime.now(_tz.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    def mk(mid, a, b, hours, tour, tier, loc, bo, streams=None):
        tier_cn = {"s": "S 级（顶级赛事）", "a": "A 级", "b": "B 级"}.get(tier, "")
        return {"id": mid, "name": "%s vs %s" % (a, b), "begin_at": iso(hours),
                "team_a": a, "team_b": b, "teams": [a, b], "tournament": tour,
                "tier": tier, "tier_cn": tier_cn, "region": "China", "location": loc,
                "bo": bo, "status": "not_started", "streams": streams or [],
                "source": "PandaScore"}

    up = [
        mk(9001, "TYLOO", "MOUZ", 1.6, "BLAST Premier World Final", "s", "中国 香港", 3,
           ["https://live.bilibili.com/"]),
        mk(9002, "Team Spirit", "Vitality", 5.5, "IEM Chengdu 2026", "s", "中国 成都", 3),
        mk(9003, "Vitality", "FaZe", 26.0, "ESL Pro League S24", "a", "马耳他", 3),
        mk(9004, "TYLOO", "Rare Atom", 30.0, "ESL 挑战者联赛", "b", "线上", 3),
    ]
    past = [
        mk(8001, "Team Spirit", "NAVI", -20, "BLAST Premier Fall Final", "s", "丹麦 哥本哈根", 3),
        mk(8002, "Vitality", "G2", -44, "IEM Chengdu 2026", "s", "中国 成都", 3),
    ]
    for m in past:
        m["status"] = "finished"
    return {"upcoming": up, "past": past, "ok": True, "error": ""}


def run(cfg, args):
    st = statemod.State(os.path.join(BASE_DIR, "data", "state.json"))

    if args.test:
        title = "CS2 赛事提醒 · 通道测试"
        md = "如果你收到这条消息，说明推送通道配置成功。\n\n关注队伍：%s" % \
             "、".join(t.label for t in cfg.teams)
        html = "<h3>推送测试成功</h3><p>关注队伍：%s</p>" % \
               "、".join(t.label for t in cfg.teams)
        results = notify.send_all(cfg, title, md, html)
        for name, ok, msg in results:
            _log(cfg, "  推送 %s %s: %s" % ("OK " if ok else "FAIL", name, msg))
        return 0 if any(ok for _, ok, _ in results) else 2

    ps, news, notes = collect(cfg)
    if args.demo:
        ps = demo_data()
        notes = [n for n in notes if "PandaScore" not in n]
        notes.append("※ 以上为演示数据；配好 PandaScore token 后即为真实赛程")
    upcoming = ps["upcoming"]
    past = ps["past"]
    _log(cfg, "拉取完成：待赛 %d 场，已结束 %d 场，新闻 %d 条" %
         (len(upcoming), len(past), len(news)))

    news = news[: int(cfg.rules.get("max_news_in_message", 8))]

    mine_up = relevant(cfg, upcoming)
    mine_past = relevant(cfg, past)
    _log(cfg, "其中涉及关注战队：待赛 %d 场，近期已结束 %d 场" % (len(mine_up), len(mine_past)))

    if args.now:
        due = [(x, 0, None) for x in mine_up]      # 立即推，不记状态
    else:
        due = pick_due(cfg, mine_up, st, args.force)
    schedule = build_team_schedule(cfg, mine_up) if cfg.rules.get("include_team_schedule", True) else {}

    if args.check or args.json:
        payload = {"upcoming_relevant": mine_up, "past_relevant": mine_past[:10],
                   "news": news, "due": [d[0] for d in due], "notes": notes}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.check:
            print("\n关注队伍: " + "、".join(t.label for t in cfg.teams))
            print("推送通道: " + (", ".join(c.get("type", "?") for c in cfg.enabled_channels()) or "未配置"))
            return 0
        return 0

    if not due:
        _log(cfg, "没有到提醒时点的比赛，不推送")
        st.save()
        return 0

    # 静默时段：除非有比赛 1 小时内就开打，否则不打扰
    if not args.now and not args.force and in_quiet_hours(cfg):
        soon = [x for x in due
                if (fmtmod.hours_until(x[0].get("begin_at", ""), cfg) or 99) <= 1]
        if not soon:
            _log(cfg, "处于静默时段 %s，跳过本次推送" % (cfg.rules.get("quiet_hours"),))
            st.save()
            return 0
        due = soon
        _log(cfg, "静默时段，但有 %d 场 1 小时内开打，仍然推送" % len(soon))

    due.sort(key=lambda x: x[0].get("begin_at") or "")
    pushed = [m for m, _, _ in due][: int(cfg.rules.get("max_matches_per_push", 10))]

    title, md, html = fmtmod.build(cfg, pushed, news, schedule, notes)
    _log(cfg, "准备推送 %d 场比赛" % len(pushed))

    if args.dry_run:
        print("\n" + "=" * 60)
        print(md)
        print("=" * 60 + "\n(以上为 --dry-run，未真正发送)")
        return 0

    results = notify.send_all(cfg, title, md, html)
    success = False
    for name, ok, msg in results:
        _log(cfg, "  推送 %s %s: %s" % ("OK " if ok else "FAIL", name, msg))
        success = success or ok

    if not success:
        _log(cfg, "所有通道都失败，本次不标记已发送（下次会重试）")
        return 2
    if not args.now:
        for _, th, key in due:
            if key:
                st.mark_sent(key, {"h": th})
        st.save()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="CS2 赛事提醒")
    ap.add_argument("--config", help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发送")
    ap.add_argument("--force", action="store_true", help="忽略已发送记录，强制再发一次")
    ap.add_argument("--test", action="store_true", help="发送一条通道测试消息")
    ap.add_argument("--check", action="store_true", help="检查配置与数据源")
    ap.add_argument("--demo", action="store_true", help="用演示数据预览推送效果")
    ap.add_argument("--now", action="store_true",
                    help="立即推送关注队伍的全部待赛赛程（忽略提醒时点，不记入已发送）")
    ap.add_argument("--qq-discover", action="store_true",
                    help="列出 QQ 机器人所在的频道和子频道 ID")
    ap.add_argument("--qq-capture", action="store_true",
                    help="连接 QQ 机器人网关，抓取群 / 单聊的 openid")
    ap.add_argument("--ask", metavar="一句话", help="智能问答：模拟在 QQ 里说一句话（调试用）")
    ap.add_argument("--results", action="store_true", help="检查关注队伍的比赛结果，有新结果就推送")
    ap.add_argument("--warm", action="store_true", help="预热所有缓存（战队索引/选手索引/HLTV 排名），让后续查询秒回")
    ap.add_argument("--build-players", type=int, default=0, metavar="页数", help="从 HLTV 抓取活跃选手总表（0=一直翻到底）")
    ap.add_argument("--query", metavar="名字", help="智能查询：自动判断是战队还是选手（调试用）")
    ap.add_argument("--team", metavar="队名", help="直接输出某支战队的报告（调试用）")
    ap.add_argument("--player", metavar="选手", help="直接输出某位选手的报告（调试用）")
    ap.add_argument("--listen", action="store_true",
                    help="常驻监听：在 QQ 里 @机器人 说「赛事推送」就回复当天赛程")
    ap.add_argument("--seconds", type=int, default=0,
                    help="--listen 最长运行秒数（0=一直跑）")
    ap.add_argument("--wait", type=int, default=180,
                    help="--qq-capture 等待秒数（默认 180）")
    ap.add_argument("--json", action="store_true", help="输出原始数据 JSON")
    args = ap.parse_args(argv)

    try:
        cfg = cfgmod.load(args.config)
    except Exception as e:
        print("配置加载失败: %s" % e, file=sys.stderr)
        return 1

    if args.qq_discover:
        return qq_discover(cfg)
    if args.qq_capture:
        return qq_capture_cmd(cfg, args)
    if args.ask is not None:
        print(_smart_ask(cfg, args.ask, "debug"))
        return 0
    if args.build_players:
        return build_players_cmd(cfg, args)
    if args.warm:
        return warm_cmd(cfg, args)
    if args.results:
        return results_cmd(cfg, args)
    if args.query:
        print(_auto_cmd(cfg, args.query))
        return 0
    if args.team:
        print(_team_cmd(cfg, args.team))
        return 0
    if args.player:
        print(_player_cmd(cfg, args.player))
        return 0
    if args.listen:
        return qq_listen_cmd(cfg, args)

    try:
        return run(cfg, args)
    except Exception:
        _log(cfg, "运行异常:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
