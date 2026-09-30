# CS2 赛事提醒（云端版）

关注 **天禄 TYLOO / 小蜜蜂 Vitality / 绿龙 Team Spirit**，赛前自动推送到 QQ。

**跑在 GitHub Actions 上**——不用开电脑、不用服务器、完全免费。

## 工作原理

```
GitHub Actions 定时触发（每 30 分钟）
        ↓
BLAST.tv 拉赛程 + 5EPlay/Steam 拉新闻
        ↓
过滤关注战队的比赛
        ↓
赛前 24h / 2h → 调 QQ 官方机器人 API 推送
        ↓
你的 QQ 私聊
```

## 需要的 Secrets

在仓库 **Settings → Secrets and variables → Actions** 里添加：

| 名称 | 说明 |
|---|---|
| `QQ_APPID` | QQ 开放平台机器人的 AppID |
| `QQ_SECRET` | 机器人的 AppSecret |
| `QQ_OPENID` | 你的 QQ 单聊 openid（私聊推送用） |
| `QQ_GROUP_OPENID` | 可选，想推到群就填群 openid |

## 交互式回复（在 QQ 里问）

工作流 `.github/workflows/listen.yml` 会让 GitHub 拉起一个 **55 分钟的 WebSocket 长连接**，
这期间你在 QQ 里 @机器人 说「**赛事推送**」，它就会回复当天赛程。

**⚠️ 请把仓库设为 Public**：私有仓库每月只有 2000 分钟免费额度，
长时间监听会跑光；公开仓库不限时长（代码里没有密钥，公开是安全的）。

**覆盖率约 92%** —— 每小时有约 5 分钟在重启的空档。
如果发了没反应，去 **Actions → CS2 指令监听 → Run workflow** 手动拉一次即可。

## 手动跑一次

仓库 **Actions** → 左侧选 **CS2 赛事提醒** → **Run workflow** → 选模式：

| 模式 | 作用 |
|---|---|
| normal | 正常：到提醒时点才推 |
| now | **立即推送**关注队伍全部待赛赛程 |
| test | 发一条通道测试消息 |
| check | 只检查数据源，不推送 |
| preview | 打印要推的内容但不发送 |

## 改关注队伍

编辑 `config.json` 的 `teams` 字段，加中文别名（新闻里常写"小蜜蜂""绿龙"）：

```json
{ "name": "NAVI", "cn": "天生赢家", "aliases": ["navi", "天生赢家"] }
```

## 注意

- GitHub 定时任务在仓库**连续 60 天无提交**后会自动停用，记得偶尔点一下
- 定时会有几分钟延迟，属正常
- 本程序**零依赖**，只用 Python 标准库
