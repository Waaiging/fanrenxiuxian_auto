# 南宫阙 · 月殿血誓自动化

实现入口为 `nangongque_boss.py`，走位和按钮决策为 `nangongque_strategy.py`。四个完整账号入口和小号/Waaiging 的受限待机入口均安装监听器；Waaiging 复用主号入口，按自身 `account_key` 运行。

## 协议来源

2026-09-16 只读核对了 Telegram 真实开场公告和[官方页面](https://asc.aiopenai.app/miniapp/xianxia-nangongque-boss)。公告为“世界通告｜月殿血誓开启”，按钮为“进入月殿战场”，使用每场动态 `nqb_` 入口。用户提供的具体入口不写死在业务代码中。

页面公开协议如下。服务端响应才是战斗、结算及奖励的依据；前端本地演练的伤害、随机技能和奖励计算没有移植到自动化中。

| 阶段 | 官方接口 | 处理方式 |
| --- | --- | --- |
| 认证与选身份 | `POST /api/miniapp/xianxia-nangongque-boss/start` | Telegram 原会话取得 initData；使用已确认 playerId，或明确匹配所选身份的 identityChoices |
| 入场验证 | 官方页面 Turnstile | action 为 `nangongque_world_boss_start`；真实浏览器回调只取用一次，随后记录 `/start` 回执 |
| 实时状态 | `/ws-ticket` 与 `/ws/miniapp/xianxia-nangongque-boss/state` | 一次性 ticket 只用于原站 WebSocket，读取服务端状态序号；断线时 HTTP `/state` 补查并有限退避重连 |
| 移动与按钮 | `/input` | `seq`、真实当前时间、归一化移动向量、`attack/dodge/mechanic`；遵守服务端冷却和客户端冷却下限 |
| 结算领奖 | `/claim` | 仅在收到结算后执行；超时保留原会话并查询 `claimed` 后恢复 |

60 秒是公告的集结窗口，重复或编辑消息不会重置它。浏览器验证与入场重试均受这一期限约束。房间不足十人、服务端关闭或验证未及时完成会如实记录，不能保证仅凭四个账号开战。

## 设置与生命周期

`automation_settings.json` 的 `nangongque_boss.participants` 独立于 `world_boss.participants`，默认四账号主魂，每账号最多一身份。旧设置加载时补齐此区段；旧版调用保存其他设置时保留已保存的南宫阙选择。Dashboard 提供参战身份单选及关闭选项，身份指令面板使用 `miniapp:nangongque-boss` 键。

配置文件可设置 `nangongque_boss.enabled=false` 关闭该账号的整个监听器。日常通过 Dashboard 关闭参战身份；这个开关会在认证和验证等待后再次检查，已入场房间保留原身份继续处理与领奖。

每账号进程锁 `.nangongque_boss_<account>.lock` 防止完整与待机 worker 同时操纵房间。私有 `.nangongque_boss_recovery/` 保存会话、原入口、输入序号及待领结算，目录权限 700、文件权限 600，最长保留至公告后 30 分钟。输入序号在发送前落盘；重启或响应不明时先读取原房间，使用更大的新序号，不重复发送原按钮。结算完成后的短期终态记录继续阻止另一进程重复入场。

普通账号 state 中新增：

- `nangongque_boss_monitor_active`、`nangongque_boss_monitor_started_at`。
- `nangongque_boss_last_status`、`nangongque_boss_last_error`、`nangongque_boss_last_updated_at`。
- `nangongque_boss_events`，最多 20 场；包括来源群、消息编号、入口摘要、身份、房间、阶段诊断与实际结算。

诊断中的 `requested_actions` 是请求次数，不是服务端成功次数。得分、排名、伤害、材料与 `automationReview` 均取真实回执；自动化不伪造战报，也不修改服务端风控系数。`completed` 表示房间流程结算并确认领奖，胜负单独读取 `settlement.success`。

## 验证范围

回归使用明确标注的合成房间响应，覆盖可信公告与多群去重、四阶段机制和危险区避让、过期状态停止移动、冷却、身份及暂停控制、原房间恢复、输入落盘与取消、领奖超时后的状态核对、敏感字段过滤、独立设置和跨副本验证页面切换。同时执行青元子、受限待机、身份面板及 Dashboard 相关回归。

开发验证不主动创建世界事件或发送群指令。真实房间的阶段推进、战斗成绩与掉落仍须在自然开场后核对；本地策略测试不能替代实战成绩。
