"""推送通道：QQ(OneBot) / QQ邮箱 / PushPlus / 企业微信 / WxPusher / Server酱。

全部只用标准库，不需要 pip 安装任何东西。
"""
from __future__ import annotations

import smtplib
import ssl
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText

from . import qqapi, web


class NotifyError(RuntimeError):
    pass


# ---------------------------------------------------------------- 各通道实现

def _pushplus(ch: dict, title: str, md: str, html: str) -> str:
    token = (ch.get("token") or "").strip()
    if not token:
        raise NotifyError("pushplus 缺少 token")
    r = web.post_json("https://www.pushplus.plus/send",
                      {"token": token, "title": title, "content": html,
                       "template": "html"})
    if r.get("code") not in (200, "200"):
        raise NotifyError("pushplus 返回: %s" % r)
    return "pushplus ok"


def _wecom(ch: dict, title: str, md: str, html: str) -> str:
    hook = (ch.get("webhook") or "").strip()
    if not hook:
        raise NotifyError("企业微信缺少 webhook")
    content = "## %s\n%s" % (title, md)
    r = web.post_json(hook, {"msgtype": "markdown",
                             "markdown": {"content": content[:4000]}})
    if r.get("errcode") != 0:
        raise NotifyError("企业微信返回: %s" % r)
    return "wecom ok"


def _wxpusher(ch: dict, title: str, md: str, html: str) -> str:
    app_token = (ch.get("appToken") or "").strip()
    uids = ch.get("uids") or []
    if not app_token or not uids:
        raise NotifyError("wxpusher 需要 appToken 和 uids")
    payload = {"appToken": app_token, "content": html, "summary": title[:99],
               "contentType": 2, "uids": uids}
    r = web.post_json("https://wxpusher.zjiecode.com/api/send/message", payload)
    if not r.get("success"):
        raise NotifyError("wxpusher 返回: %s" % r)
    return "wxpusher ok"


def _serverchan(ch: dict, title: str, md: str, html: str) -> str:
    key = (ch.get("sendkey") or "").strip()
    if not key:
        raise NotifyError("Server酱缺少 sendkey")
    url = "https://sctapi.ftqq.com/%s.send" % key
    body = urllib.parse.urlencode({"title": title, "desp": md}).encode()
    text = web.request(url, data=body, method="POST", retries=1, timeout=25,
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    if '"code":0' not in text.replace(" ", ""):
        raise NotifyError("Server酱返回: %s" % text[:200])
    return "serverchan ok"


def _qqmail(ch: dict, title: str, md: str, html: str) -> str:
    host = ch.get("smtp_host") or "smtp.qq.com"
    port = int(ch.get("smtp_port") or 465)
    user = (ch.get("user") or "").strip()
    auth = (ch.get("authcode") or "").strip()
    to = (ch.get("to") or user).strip()
    if not user or not auth:
        raise NotifyError("QQ邮箱需要 user（QQ邮箱地址）和 authcode（SMTP 授权码）")

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = Header("CS2 赛事提醒 <%s>" % user, "utf-8")
    msg["To"] = to

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=25, context=ctx) as s:
            s.login(user, auth)
            s.sendmail(user, [x.strip() for x in to.split(",") if x.strip()], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=25) as s:
            s.starttls(context=ctx)
            s.login(user, auth)
            s.sendmail(user, [x.strip() for x in to.split(",") if x.strip()], msg.as_string())
    return "qqmail ok -> %s" % to


def _qqbot(ch, title, md, html) -> str:
    """通过 OneBot v11 协议发到 QQ（NapCat / LLOneBot / Lagrange 等）。

    config.json 里填：
        "type": "qqbot",
        "api": "http://127.0.0.1:3000",
        "access_token": "NapCat 里设置的那个 token（没设就留空）",
        "user_id": "你的QQ号",          // 私聊发给自己
        "group_id": ""                  // 或者发到群（填了群号就优先发群）
    """
    api = (ch.get("api") or "http://127.0.0.1:3000").rstrip("/")
    token = (ch.get("access_token") or "").strip()
    uid = str(ch.get("user_id") or "").strip()
    gid = str(ch.get("group_id") or "").strip()
    if not uid and not gid:
        raise NotifyError("QQ 通道需要填 user_id（你的QQ号）或 group_id（群号）")

    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token

    # QQ 不渲染 Markdown，用纯文本 + 截断，避免刷屏
    text = title + "\n\n" + md
    text = text.replace("**", "").replace("##", "").replace("###", "")
    if len(text) > 1800:
        text = text[:1800] + "\n\n…（内容过长已截断，完整版请看本次日志）"

    if gid:
        url, payload = api + "/send_group_msg", {"group_id": int(gid), "message": text}
    else:
        url, payload = api + "/send_private_msg", {"user_id": int(uid), "message": text}

    r = web.post_json(url, payload, headers=headers, timeout=20)
    if r.get("status") not in ("ok", "async") and r.get("retcode") != 0:
        raise NotifyError("OneBot 返回: %s" % str(r)[:200])
    who = ("群 " + gid) if gid else ("QQ " + uid)
    return "qqbot ok -> %s" % who


def _qqofficial(ch, title, md, html) -> str:
    """腾讯官方 QQ 机器人（QQ 频道 / 群 / 单聊）。

    config.json 里填：
        "type": "qqofficial",
        "appId": "102xxxxxx",          // 开放平台「开发设置」页
        "clientSecret": "xxxxxx",      // 同上
        "channel_id": "1234567"        // 发到哪个子频道（用 run.bat --qq-discover 查）
        // 或者 "group_openid": "..."  /  "user_openid": "..."
    """
    appid, secret = qqapi.split_credential(str(ch.get("appId") or ""),
                                           str(ch.get("clientSecret") or ""))
    cid = str(ch.get("channel_id") or "").strip()
    gid = str(ch.get("group_openid") or "").strip()
    uid = str(ch.get("user_openid") or "").strip()
    if not (cid or gid or uid):
        raise NotifyError("官方QQ机器人需要填 channel_id（频道）或 group_openid / user_openid")

    token = qqapi.get_token(appid, secret)

    text = title + "\n\n" + md
    text = text.replace("**", "").replace("##", "").replace("###", "")
    if len(text) > 1800:
        text = text[:1800] + "\n…（已截断）"

    if cid:
        qqapi.send_channel(cid, text, appid, token)
        return "qqofficial ok -> 频道 " + cid
    if gid:
        qqapi.send_group(gid, text, appid, token)
        return "qqofficial ok -> 群 " + gid
    qqapi.send_c2c(uid, text, appid, token)
    return "qqofficial ok -> 单聊 " + uid


CHANNELS = {
    "qqofficial": _qqofficial,
    "qqbot": _qqbot,
    "pushplus": _pushplus,
    "wecom": _wecom,
    "wxpusher": _wxpusher,
    "serverchan": _serverchan,
    "qqmail": _qqmail,
}


def send_all(cfg, title: str, markdown: str, html: str) -> list:
    """按配置依次推送，返回 [(通道, 是否成功, 说明)]，单通道失败不影响其它。"""
    results = []
    channels = cfg.enabled_channels()
    if not channels:
        results.append(("(未配置)", False, "config.json 里没有任何 enabled=true 的推送通道"))
        return results
    for ch in channels:
        name = ch.get("type", "?")
        fn = CHANNELS.get(name)
        if not fn:
            results.append((name, False, "未知通道类型"))
            continue
        try:
            results.append((name, True, fn(ch, title, markdown, html)))
        except Exception as e:
            results.append((name, False, "%s: %s" % (type(e).__name__, e)))
    return results
