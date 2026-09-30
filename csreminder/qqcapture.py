"""连接 QQ 机器人网关，抓取群 / 单聊的 openid。

腾讯没有「列出我加入的群」的接口，所以只能用这个办法：
机器人连上网关后，你在群里 @ 它一下（或私聊它一句），
平台就会推一条事件过来，里面带着 group_openid / user_openid。
"""
from __future__ import annotations

import json
import socket
import time

from . import qqapi, ws

GATEWAY = "/gateway"
# 1<<25 = 群与单聊事件；1<<0 = 频道基础事件
INTENTS = (1 << 25) | (1 << 0)

EVENT_GROUP = "GROUP_AT_MESSAGE_CREATE"
EVENT_C2C = "C2C_MESSAGE_CREATE"
EVENT_GUILD = "AT_MESSAGE_CREATE"


def capture(appid: str, secret: str, wait_seconds: int = 180, log=print) -> dict:
    """阻塞等待，返回 {"groups": {...}, "users": {...}, "guild_channels": {...}}。"""
    appid, secret = qqapi.split_credential(appid, secret)
    token = qqapi.get_token(appid, secret, force=True)

    info = qqapi.api_get(GATEWAY, appid, token)
    url = info.get("url")
    if not url:
        raise RuntimeError("拿不到网关地址: %s" % info)

    found = {"groups": {}, "users": {}, "guilds": {}}
    log("连接网关 %s ..." % url)
    c = ws.WsClient(url, timeout=15)
    try:
        # 1) Hello
        op, text = c.recv()
        hello = json.loads(text) if text else {}
        hb = int((hello.get("d") or {}).get("heartbeat_interval") or 30000) / 1000.0
        log("网关已连接（心跳 %.0f 秒）" % hb)

        # 2) Identify
        c.send(json.dumps({
            "op": 2,
            "d": {"token": "QQBot " + token, "intents": INTENTS,
                  "shard": [0, 1], "properties": {}},
        }))

        deadline = time.time() + wait_seconds
        next_hb = time.time() + hb
        c.settimeout(1.0)
        seq = None
        log("等待中…… 请现在去 QQ 操作（见下方提示）")

        while time.time() < deadline:
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
                if t == EVENT_GROUP:
                    gid = d.get("group_openid")
                    if gid and gid not in found["groups"]:
                        found["groups"][gid] = d.get("author", {}).get("member_openid", "")
                        log("  ★ 抓到群 group_openid = %s" % gid)
                elif t == EVENT_C2C:
                    uid = (d.get("author") or {}).get("user_openid")
                    if uid and uid not in found["users"]:
                        found["users"][uid] = ""
                        log("  ★ 抓到单聊 user_openid = %s" % uid)
                elif t == EVENT_GUILD:
                    ch = d.get("channel_id")
                    if ch and ch not in found["guilds"]:
                        found["guilds"][ch] = d.get("guild_id", "")
                        log("  ★ 抓到频道 channel_id = %s" % ch)
                elif t == "READY":
                    log("  已登录为：%s" % (d.get("user") or {}).get("username"))

            elif op == 9:
                log("  [!] 鉴权失败（intents 未获批？）")
                break
            elif op == 7:
                log("  [!] 服务器要求重连")
                break

            if found["groups"] or found["users"] or found["guilds"]:
                # 再等 2 秒收其它类型，然后收工
                time.sleep(2)
                break
    finally:
        c.close()
    return found
