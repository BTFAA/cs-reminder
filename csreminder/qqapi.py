"""腾讯官方 QQ 机器人 API（QQ 频道 / 群 / 单聊）。

鉴权：appId + clientSecret -> access_token（有效期约 2 小时，本地缓存）
发送：Authorization: QQBot <access_token>
"""
from __future__ import annotations

import json
import os
import time

from . import web

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE_DIR, "data", "qq_token.json")


class QQError(RuntimeError):
    pass


def _load_cache(appid: str):
    try:
        with open(CACHE, "r", encoding="utf-8") as f:
            c = json.load(f)
        if c.get("appid") == appid and c.get("expire_at", 0) > time.time() + 120:
            return c.get("access_token")
    except Exception:
        pass
    return None


def _save_cache(appid: str, token: str, expires_in: int):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"appid": appid, "access_token": token,
                       "expire_at": time.time() + expires_in}, f)
    except Exception:
        pass


def split_credential(appid: str, secret: str = ""):
    """兼容平台给的两种写法：

    - 分开填： appId="102123456", clientSecret="AbCdEf..."
    - 连写填： appId="102123456:AbCdEf..."（复制整条 token 时常见），secret 留空
    """
    appid = (appid or "").strip()
    secret = (secret or "").strip()
    if not secret and ":" in appid:
        a, s = appid.split(":", 1)
        return a.strip(), s.strip()
    return appid, secret


def get_token(appid: str, secret: str, force: bool = False) -> str:
    """拿 access_token，带本地缓存。"""
    appid, secret = split_credential(appid, secret)
    if not appid or not secret:
        raise QQError("需要 appId 和 clientSecret（在 QQ开放平台机器人的「开发设置」页）")
    if not force:
        cached = _load_cache(appid)
        if cached:
            return cached

    body = json.dumps({"appId": str(appid), "clientSecret": secret}).encode()
    text = web.request(TOKEN_URL, data=body, method="POST", retries=2, timeout=25,
                       headers={"Content-Type": "application/json"})
    try:
        j = json.loads(text)
    except json.JSONDecodeError:
        raise QQError("获取 token 返回非 JSON: %s" % text[:200])

    if not j.get("access_token"):
        raise QQError("获取 token 失败: %s" % json.dumps(j, ensure_ascii=False)[:250])

    token = j["access_token"]
    try:
        expires = int(j.get("expires_in") or 7200)
    except (TypeError, ValueError):
        expires = 7200
    _save_cache(appid, token, expires)
    return token


def _headers(appid: str, token: str) -> dict:
    return {"Authorization": "QQBot " + token,
            "X-Union-Appid": str(appid),
            "Content-Type": "application/json"}


def _check(text: str, what: str):
    try:
        j = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return
    if isinstance(j, dict) and (j.get("code") or j.get("err_code")) and j.get("code") != 0:
        raise QQError("%s 失败: %s" % (what, json.dumps(j, ensure_ascii=False)[:250]))


def api_get(path: str, appid: str, token: str):
    text = web.request(API_BASE + path, headers=_headers(appid, token),
                       timeout=25, retries=2)
    _check(text, "GET " + path)
    return json.loads(text) if text.strip() else {}


def api_post(path: str, payload: dict, appid: str, token: str):
    body = json.dumps(payload, ensure_ascii=False).encode()
    text = web.request(API_BASE + path, headers=_headers(appid, token), data=body,
                       method="POST", timeout=25, retries=1)
    _check(text, "POST " + path)
    return json.loads(text) if text.strip() else {}


def discover(appid: str, secret: str) -> dict:
    """列出机器人所在的频道，以及每个频道下的子频道 ID。"""
    token = get_token(appid, secret, force=True)
    guilds = api_get("/users/@me/guilds", appid, token)
    out = []
    for g in (guilds if isinstance(guilds, list) else []):
        gid, gname = g.get("id"), g.get("name")
        channels = []
        try:
            chs = api_get("/guilds/%s/channels" % gid, appid, token)
            for c in (chs if isinstance(chs, list) else []):
                channels.append({"id": c.get("id"), "name": c.get("name"),
                                 "type": c.get("type")})
        except Exception as e:
            channels.append({"error": str(e)})
        out.append({"guild_id": gid, "guild_name": gname, "channels": channels})
    return {"guilds": out}


def send_channel(channel_id: str, content: str, appid: str, token: str, msg_id: str = None):
    payload = {"content": content}
    if msg_id:
        payload["msg_id"] = msg_id
    return api_post("/channels/%s/messages" % channel_id, payload, appid, token)


def send_group(group_openid: str, content: str, appid: str, token: str,
               seq: int = 1, msg_id: str = None):
    """发群消息。带 msg_id 就是「被动回复」（QQ 群要求被动回复才不受主动消息配额限制）。"""
    payload = {"content": content, "msg_type": 0, "msg_seq": seq}
    if msg_id:
        payload["msg_id"] = msg_id
    return api_post("/v2/groups/%s/messages" % group_openid, payload, appid, token)


def send_c2c(user_openid: str, content: str, appid: str, token: str,
             seq: int = 1, msg_id: str = None):
    """发单聊消息。带 msg_id 就是被动回复。"""
    payload = {"content": content, "msg_type": 0, "msg_seq": seq}
    if msg_id:
        payload["msg_id"] = msg_id
    return api_post("/v2/users/%s/messages" % user_openid, payload, appid, token)
