"""主流程：拉取 -> 过滤关注战队 -> 组装 -> 推送。"""
from __future__ import annotations

import argparse
import json
import time
import os
import sys
import traceback
from datetime import datetime

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
    triggers = cmds.get("triggers") or ["赛事推送", "赛程"]
    triggers = [t for t in triggers if t]

    print("=" * 62)
    print("  指令监听已启动")
    print()
    print("  触发词：" + "、".join(triggers))
    print("  用法：在 QQ 里私聊机器人，或在群里 @机器人，说一句上面任意一个词")
    print("       它会立刻回复「当天赛程」")
    print()
    print("  按 Ctrl+C 停止")
    print("=" * 62)
    print()

    cache = {"at": 0, "text": ""}

    def handle(text, kind, target):
        low = (text or "").lower()
        if not any(t.lower() in low for t in triggers):
            return None
        now = time.time()
        if now - cache["at"] < 300 and cache["text"]:
            return cache["text"]
        ps = _schedule_only(cfg)
        if not ps.get("ok"):
            return "抱歉，赛程数据暂时取不到：\n%s" % (ps.get("error") or "未知错误")[:200]
        matches = list(ps.get("upcoming") or []) + list(ps.get("past") or [])
        report = fmtmod.build_day_report(cfg, matches)
        cache.update({"at": now, "text": report})
        return report

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
    if args.listen:
        return qq_listen_cmd(cfg, args)

    try:
        return run(cfg, args)
    except Exception:
        _log(cfg, "运行异常:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
