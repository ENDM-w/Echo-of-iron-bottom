# 多舰船席位平台 MVP

平台层位于 `backend/src/iron_bottom_sound/platform/`，不复制兵棋规则。它把一局规则引擎包装成多个舰船席位：每个席位独立提交订单、独立获取视野，裁判在全部席位提交后批准阶段推进。

## 本地启动

```powershell
py -3.12 -m pip install -e ".[dev]"
py -3.12 -m iron_bottom_sound
```

默认 API 为 `http://127.0.0.1:8000`。

## 最小操作流程

1. `POST /platform/users` 创建第一个裁判账号。首个账号必须设置 `is_referee=true`。
2. 登录 `POST /platform/auth/login`，返回 Bearer token。
3. 在剧本工坊制作想定；每艘舰船同时生成一个席位和一套玩家账密，随 `seat_assignments` 保存。
4. 裁判创建 `POST /platform/matches`；平台自动创建这些玩家账号并绑定单舰席位，裁判摘要会返回账号和密码。
5. 玩家查看 `GET /platform/matches/{match_id}/seats/{seat_id}/view`。
6. 玩家提交 `POST /platform/matches/{match_id}/seats/{seat_id}/orders`。
7. 裁判查看 `GET /platform/matches/{match_id}/review`，然后批准或驳回：
   - `POST /platform/matches/{match_id}/approve`
   - `POST /platform/matches/{match_id}/reject`
8. 批准后进入 `awaiting_publication`：结果仅对裁判开放。裁判可审阅骰点、重骰并重跑整次裁决，完成修正后发布：
   - `POST /platform/matches/{match_id}/judge/reroll`
   - `POST /platform/matches/{match_id}/judge/publish`
   - `PATCH /platform/matches/{match_id}/judge/ships/{ship_id}`
9. 裁判可在批准前替换任意席位订单：
   - `POST /platform/matches/{match_id}/judge/seats/{seat_id}/orders`
10. 裁判可查看任意骰点重掷和状态覆盖的完整审计：
   - `GET /platform/matches/{match_id}/judge/overrides` 查看完整覆盖审计

批准前必须完成两件事：双方都至少有一个席位，并且想定中的每条舰船都已经绑定到某个席位。旧版内置想定仍可创建为无席位对局；新制作的想定会在创建对局时自动完成绑定。

所有写操作都使用 `Authorization: Bearer <token>`。平台不接受客户端自行声明阵营；阵营和舰船归属来自裁判建立的席位绑定。

## 当前边界

- 订单和平台席位落在与规则快照相同的 SQLite 文件中，但使用独立的 `platform_*` 表。
- 登录会话暂存在进程内，服务重启后需要重新登录；后续部署时再迁移到持久化 session 或外部身份服务。
- 暂无计时器、自动推进和 AI 自动补位；阵营电报按下文规则独立结算。
- 裁判批准后，规则引擎会在同一次批准中处理移动结算、鱼雷效果、火灾结算等无订单阶段，平台只在下一个需要玩家订单的阶段重新收集；不存在按时间自动推进。
- 整回合批准后，平台保存回合开始状态与已批准订单，重骰会把替代骰值放回原随机调用位置，并由规则引擎重跑事件及所有后续效果。之前基于旧结果产生的伤害、标记、状态和后续事件会被替换，不会叠加。
- 裁判的舰船状态和视野修正会作为审理期间的修正保留在每次重跑之后。发布前玩家 API 继续读取上次已发布状态，待裁决结果、修正及待送达电报均确认后，裁判调用 `judge/publish`；发布后玩家端自动刷新并进入下一阶段。
- 裁判订单覆盖仍经过规则引擎校验；舰船状态覆盖接受完整 `ShipState` 字段并拒绝修改 `id/name/side/ship_type` 身份字段。重骰只能选择当前待发布裁决内的骰点，已发布历史骰点不可重骰。
- `platform.service.PlatformService` 是单写者边界；生产部署前还需要为同一局增加队列/数据库锁，避免多进程同时审批。

## 浏览器工作台

启动前后端后打开 `http://localhost:5173/`，首页的“多人网推工作台”入口会进入登录页。裁判登录后可以在剧本工坊制作舰船、编队和每舰账密，创建对局后查看自动生成的席位与凭据、查看订单收集状态、覆盖任意席位订单、批准/驳回阶段、审阅并重骰待发布事件、覆盖舰船状态，然后发布本回合结果。玩家登录后只能载入自己被绑定的席位，看到经过战争迷雾过滤的舰船和事件，并提交本席位的 JSON 行动；同阵营其他席位不会出现在其视野中。玩家端在裁判发布后自动刷新观察，不显示裁判审理期间的中间结果。

工作台的战术地图复用正式规则客户端的六角坐标、舰船棋子、鱼雷航迹和缩放/平移控件。玩家地图只接收本席位的战争迷雾视图；可见敌舰在平台层进一步脱敏为“驱逐甲/巡洋甲/战列甲”等规模标签，并使用 `DD标记.png`、`CA-CL标记.png`、`BB标记.png`，不会把真实舰名、具体舰种或原始算子下发给玩家。裁判地图通过受保护的 `/platform/matches/{match_id}/judge/view` 接口显示完整态势。

前端生产构建：`pnpm run build`。API 文档：`http://127.0.0.1:8000/docs`。
## 阵营电报

玩家席位每回合可封存或更新一则电报，正文只会发给同阵营的其他席位。电报在对局进入下一回合时按每个接收席位独立掷 D100；开启“风暴降临”时掷 D50。正文去除空白后的 Unicode 字符数严格小于骰值才会送达。接收失败只显示失败提示，不返回正文。规则编号为 `IBS-R-COMMS-01`；电报正文和带规则编号的逐接收席位骰值/结果写入平台 SQLite。玩家只能读取本席位收件箱，裁判可查看全部正文、收件人和骰点。

## 局外舰船资料库

首页“舰船资料库”入口由裁判账号登录后可用，与任何对局无关。导入时选择单舰 JSON（顶层包含 `catalog` 与 `record`）和对应算子图片。`record` 必须是完整且通过 `ShipRecord` 校验的规则档案，至少包含一个主炮炮位；`catalog` 提供名称、英文名、舰级、舰型和来源表/行。登录后可下载由 API 生成的 `ship-import.schema.json` 字段约束。算子图片仅接受 PNG/JPEG/WebP，文件不超过 8 MiB，服务端会检查图片格式、尺寸并实际解码，再按 EXIF 方向修正、等比缩放并居中透明补边到统一 224×144 px PNG 画布；不会拉伸主体。规范化图片字节与档案一并存入 `imported_ship_records` 表。目录将返回 `db:IBS-U-...` 资源标识，图片由 `/ships/{ship_id}/asset` 提供，并可在目录、想定工坊及地图中显示。静态原始资料与结构化 YAML 不会被改写。ID 需以 `IBS-U-` 开头且不能与现有名录或档案冲突；重复 ID 不覆盖。
