"""自然语言理解：把 QQ 里发来的一句话，解析成「意图 + 实体」。

设计目标（对应需求）：
    · 免前缀     —— 直接说「donk」「天禄」也能认
    · 容错纠错   —— 错别字 / 拼音 / 大小写 / 多余空格都能匹配
    · 自然问句   —— 「天禄下一场打谁」「绿龙最近赢了吗」这类问法
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 意图词表
INTENT_WORDS = {
    "ranking": [
        "世界排名", "排行榜", "排行", "排名", "榜单", "世界第一", "前十", "前10",
        "top10", "top20", "top", "最强战队", "谁最强", "谁是第一", "谁第一",
    ],
    "compare": [
        "对比", "比较", "谁更", "谁强", "谁厉害", "pk", "PK", "单挑", "vs",
    ],
    "schedule": [
        "赛程", "比赛安排", "什么时候打", "什么时候比赛", "下一场", "下场",
        "几点打", "几点开始", "今天比赛", "明天比赛", "即将开始", "有什么比赛",
        "打谁", "对手是谁", "比赛时间",
    ],
    "result": [
        "比分", "结果", "赢了", "输了", "赢了吗", "输了吗", "战绩",
        "最近战绩", "打得怎么样", "打完了吗", "赛果", "最近怎么样", "状态",
    ],
    "help": ["帮助", "怎么用", "指令", "菜单", "能干什么", "会什么", "用法"],
}
INTENTS = list(INTENT_WORDS.keys())

# 句子里出现这些词时，明确锁定实体类型
FORCE_PLAYER = ["选手", "队员", "player"]
FORCE_TEAM = ["战队", "队伍", "队", "team"]


def normalize(s: str) -> str:
    """去掉空格和标点、转小写，用于宽松比对。"""
    return re.sub(r"[\s\-_.·、,，。!！?？@#:：/\\\\|()（）\[\]【】\"'“”‘’]+", "", (s or "").lower())


def levenshtein(a: str, b: str, cap: int = 3) -> int:
    """编辑距离（带提前退出，超过 cap 就不再算）。"""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            if v < best:
                best = v
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def detect_intent(text: str) -> str:
    """识别意图。返回 ranking / compare / schedule / result / help / 空字符串。"""
    low = (text or "").lower()
    norm = normalize(text)
    for intent in ("compare", "ranking", "schedule", "result", "help"):
        for kw in INTENT_WORDS[intent]:
            k = kw.lower()
            if k in low or normalize(k) in norm:
                return intent
    return ""


def detect_forced_kind(text: str) -> str:
    """句子里的「选手」「战队」等词决定实体类型。"""
    norm = normalize(text)
    for w in FORCE_PLAYER:
        if normalize(w) in norm:
            return "player"
    for w in FORCE_TEAM:
        if normalize(w) in norm:
            return "team"
    return ""


def _tokens(text: str):
    """切出可能的「名字」片段。"""
    s = re.sub(r"[\s,，。!！?？@#:：/\\\\|()（）\[\]【】\"'“”‘’]+", " ", text or "")
    return [t for t in s.split(" ") if t]


def match_entity(text: str, cfg, player_index=None, allow_fuzzy: bool = True):
    """从文本里找出战队或选手。

    返回 (kind, name, how)：
        kind  : "team" | "player" | ""
        name  : 匹配到的原始名字（队名 slug 或选手名）
        how   : "exact" | "fuzzy" | "context" | ""
    """
    norm_text = normalize(text)
    if not norm_text:
        return "", "", ""

    # ---------- 收集候选 ----------
    teams = {}          # 归一化名字 -> (slug, 显示名)
    try:
        from .sources import blastteams
        idx = blastteams.load_team_index()
        for slug in idx:
            teams[normalize(slug)] = (slug, slug.replace("-", " ").title())
    except Exception:
        idx = {}
    # 关注队伍的中英文名和别名（含拼音）
    alias2slug = {}
    for t in cfg.teams:
        want = t.name.lower().replace(" ", "-")
        slug = None
        for s in idx:
            if s == want or s.replace("-", "") == want.replace("-", ""):
                slug = s
                break
        for a in [t.name, t.cn] + list(t.aliases):
            if not a:
                continue
            teams[normalize(a)] = (slug or want, t.label)
            if slug:
                alias2slug[normalize(a)] = slug

    players = {}
    if player_index:
        for k, v in player_index.items():
            players[normalize(k)] = v

    # ---------- 1) 子串匹配（要够长才认，避免 "pr" 命中 "dupreeh"）----------
    def _ok_sub(cand: str, textlen: int) -> bool:
        cjk = bool(re.search(r"[\u4e00-\u9fff]", cand))
        if cjk:
            return len(cand) >= 2          # 中文队名「天禄」「绿龙」允许 2 字
        # 西文名字：至少 3 字，而且长度要和整句接近（避免短片段命中长词）
        return len(cand) >= 3 and abs(len(cand) - textlen) <= 2

    for src, kind in ((teams, "team"), (players, "player")):
        hits = [(n, v) for n, v in src.items()
                if _ok_sub(n, len(norm_text)) and n in norm_text]
        if hits:
            hits.sort(key=lambda x: -len(x[0]))
            n, v = hits[0]
            if kind == "team":
                return "team", v[0], "exact"
            return "player", v["name"], "exact"

    if not allow_fuzzy:
        return "", "", ""

    # ---------- 2) 模糊匹配（错别字 / 拼音 / 大小写）----------
    cands = []
    for n, v in teams.items():
        if len(n) >= 4:
            cands.append((n, "team", v[0]))
    for n, v in players.items():
        if len(n) >= 4:
            cands.append((n, "player", v["name"]))

    best = None
    for tok in _tokens(text):
        tn = normalize(tok)
        if len(tn) < 3 or tn in INTENT_WORDS.get("help", []) or len(tn) > 20:
            continue
        for cn, kind, val in cands:
            # 长度差太大直接跳过
            if abs(len(cn) - len(tn)) > 2:
                continue
            d = levenshtein(tn, cn, cap=2)
            limit = 1 if len(tn) <= 4 else 2
            if d <= limit:
                score = (d, -len(cn))
                if best is None or score < best[0]:
                    best = (score, kind, val, cn)
    if best:
        return best[1], best[2], "fuzzy"

    return "", "", ""


def describe(text: str, cfg, player_index=None, last=None):
    """总入口：返回解析结果 dict。

    last: 上一条的上下文 {"kind":..., "name":...}
    """
    intent = detect_intent(text)
    forced = detect_forced_kind(text)
    kind, name, how = match_entity(text, cfg, player_index)

    # 没找到实体时，只有「看起来像追问」才沿用上一条，否则会乱套
    #   · 有指代词（那/他/它/这个/那个）
    #   · 或者本身带意图（「下一场呢」「战绩呢」）
    follow_words = bool(re.search(r"(那|他|她|它|这个|那个|呢|还)", text or ""))
    if (not name and last and last.get("name")
            and follow_words
            and intent in ("schedule", "result", "team", "player", "")):
        return {"intent": intent or last.get("intent", ""), "kind": last["kind"],
                "name": last["name"], "how": "context", "forced": forced}

    if forced and name:
        kind = forced
    return {"intent": intent, "kind": kind, "name": name, "how": how, "forced": forced}
