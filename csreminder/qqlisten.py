"""常驻监听 QQ 机器人消息，响应「赛事推送」这类指令。

用 WebSocket 长连接（腾讯官方网关）。断线自动重连。
收到消息 -> 调 handler(text, kind, target) -> 返回文本就当作被动回复发回去。
"""
from __future__ import annotations

import json
import re
import socket
import time

from . import qqapi, ws

GATEWAY = "/gateway"
# 1<<25 群与单聊事件；1<<0 频道基础事件
INTENTS = (1 << 25) | (1 << 0)

EVENT_GROUP = "GROUP_AT_MESSAGE_CREATE"
EVENT_C2C = "C2C_MESSAGE_CREATE"
EVENT_GUILD = "AT_MESSAGE_CREATE"

# 去掉 @机器人 留下的标记
RE_MENTION = re.compile(r"<@!?\d+>|<qqbot-at-user[^>]*/?>|<@[^>]*>")
RE_SPACE = re.compile(r"\s+")


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = RE_MENTION.sub(" ", s)
    return RE_SPACE.sub(" ", s).strip()


def listen(appid: str, secret: str, handler, log=print,
           on_ready=None, max_seconds: int = 0):
    """常驻监听。

    handler(text, kind, target) -> str | None
        kind:   "c2c" | "group" | "guild"
        target: user_openid / group_openid / channel_id
        返回非空字符串就作为被动回复发回；返回 None 表示不理会。
    max_seconds: >0 时限时退出（测试用），0 表示一直跑。
    """
    appid, secret = qqapi.split_credential(appid, secret)
    started = time.time()

    while True:
        if max_seconds and time.time() - started > max_seconds:
            log("达到时限，退出监听")
            return 0
        try:
            token = qqapi.get_token(appid, secret, force=True)
            info = qqapi.api_get(GATEWAY, appid, token)
            url = info.get("url")
            if not url:
                raise RuntimeError("拿不到网关地址: %s" % info)

            log("连接网关 %s" % url)
            c = ws.WsClient(url, timeout=15)
            try:
                deadline = (started + max_seconds) if max_seconds else 0
                _session(c, appid, token, handler, log, on_ready, deadline)
            finally:
                c.close()
        except KeyboardInterrupt:
            log("收到中断，退出")
            return 0
        except Exception as e:
            log("连接异常（%s: %s），5 秒后重连" % (type(e).__name__, str(e)[:120]))
            time.sleep(5)


def _session(c, appid: str, token: str, handler, log, on_ready, deadline: float = 0):
    """一次连接的生命周期。返回表示连接结束，由外层重连。"""
    op, text = c.recv()
    hello = json.loads(text) if text else {}
    hb = int((hello.get("d") or {}).get("heartbeat_interval") or 30000) / 1000.0
    log("网关已连接（心跳 %.0f 秒）" % hb)

    c.send(json.dumps({
        "op": 2,
        "d": {"token": "QQBot " + token, "intents": INTENTS,
              "shard": [0, 1], "properties": {}},
    }))

    seq = None
    next_hb = time.time() + hb
    c.settimeout(1.0)
    ready = False

    while True:
        if deadline and time.time() > deadline:
            log("达到时限，停止监听")
            return
        if time.time() >= next_hb:
            c.send(json.dumps({"op": 1, "d": seq}))
            next_hb = time.time() + hb

        try:
            op, text = c.recv()
        except socket.timeout:
            continue

        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue

        op = msg.get("op")
        if op == 0:
            seq = msg.get("s", seq)
            t = msg.get("t")
            d = msg.get("d") or {}

            if t == "READY":
                ready = True
                log("已登录为：%s  开始监听指令…" % (d.get("user") or {}).get("username"))
                if on_ready:
                    try:
                        on_ready(d)
                    except Exception:
                        pass
                continue

            kind = target = msg_id = None
            if t == EVENT_C2C:
                kind = "c2c"
                target = (d.get("author") or {}).get("user_openid")
            elif t == EVENT_GROUP:
                kind = "group"
                target = d.get("group_openid")
            elif t == EVENT_GUILD:
                kind = "guild"
                target = d.get("channel_id")

            if not (kind and target):
                continue

            msg_id = d.get("id")
            raw = d.get("content") or ""
            text_in = clean_text(raw)
            who = (d.get("author") or {}).get("member_openid") or \
                  (d.get("author") or {}).get("user_openid") or ""
            log("收到[%s] %s: %s" % (kind, str(who)[:12], text_in[:60]))

            try:
                reply = handler(text_in, kind, target)
            except Exception as e:
                log("  处理失败: %s: %s" % (type(e).__name__, str(e)[:120]))
                reply = None

            if not reply:
                continue

            try:
                if len(reply) > 1500:
                    reply = reply[:1500] + "\n…（内容过长已截断）"
                if kind == "group":
                    qqapi.send_group(target, reply, appid, token, seq=1, msg_id=msg_id)
                elif kind == "c2c":
                    qqapi.send_c2c(target, reply, appid, token, seq=1, msg_id=msg_id)
                else:
                    qqapi.send_channel(target, reply, appid, token, msg_id=msg_id)
                log("  已回复（%d 字）" % len(reply))
            except Exception as e:
                log("  回复失败: %s: %s" % (type(e).__name__, str(e)[:150]))

        elif op == 9:
            log("鉴权失败（intents 未获批？），退出本次连接")
            return
        elif op == 7:
            log("服务器要求重连")
            return
