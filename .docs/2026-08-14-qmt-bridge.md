# 2026-08-14 QMT Bridge 只读连接器

分支：`dev`  
提交：`76dcb0e` feat: add read-only QMT Bridge connector and recognize QMT accounts

## 变更摘要

接入 Windows 侧 **QMT Bridge**（`qmt-server` HTTP API），把 A 股资金账户做成与 Longbridge / Futu 同级的 `broker_sdk` 连接器。本层只做监控：读账户、持仓、当日委托/成交、行情与 K 线；**不下单、不撤单**。

同时把 CLI、Agent 工具、Runtime 页、桌面凭据白名单里所有「账户」相关入口对齐 QMT 的字段（`assets` / `qty` / `account_id`），避免出现「无账户摘要」或空持仓。

## 新增能力

### 连接器

| 档案 ID | 环境 | 能力 |
| --- | --- | --- |
| `qmt-paper-sdk` | 操作员声明的模拟/纸面账户 | 只读 |
| `qmt-live-sdk-readonly` | 操作员声明的实盘账户 | 只读 |

可读接口：

- 资金账户：`GET /api/trading/asset`（现金、可用、市值、总资产、冻结、CNY）
- 持仓：`GET /api/trading/positions`
- 当日委托：`GET /api/trading/orders`；可选当日成交：`GET /api/trading/trades`
- 行情快照：`GET /api/market/full_tick`
- 历史 K 线：`GET /api/history`

代码分类：`.SH` / `.SZ` 按 A 股（`cn_equity`）处理。Bridge 启动时绑定一个 `--account-id`，没有运行时可核验的模拟/实盘分界，因此不下单（与 Longbridge / Trading 212 同一安全口径）。

### 账户识别（本次补齐）

QMT 原生返回 `assets[]`、`qty`、扁平订单。现在同时映射到系统已有形状，各入口都能认出同一账户：

- `balances`（净资/现金/购买力）
- 扁平 `account`（`cash` / `equity` / `buying_power` / `account_number`）
- CLI `--account` / 工具参数 `account` → Bridge `account_id`
- CLI 账户 / 持仓 / 订单 / 行情表能渲染 QMT 字段
- Agent `trading_account`、聊天组合摘要 prompt 能读 QMT 账户形态
- Runtime 未配置时列出 `QMT_BRIDGE_HOST` / `PORT` / `API_KEY` / `ACCOUNT_ID`
- 桌面端把 `QMT_BRIDGE_API_KEY` 纳入安全凭据白名单

### 配置

在 `agent/.env`（模板见 `agent/.env.example`）：

```
QMT_BRIDGE_HOST=127.0.0.1   # 或 Windows 主机 LAN IP，禁止 0.0.0.0
QMT_BRIDGE_PORT=8000
QMT_BRIDGE_API_KEY=...
QMT_BRIDGE_ACCOUNT_ID=...
```

Windows：QMT（独立交易）登录后启动  
`qmt-server --trading --api-key ... --account-id ...`  
本机/其他 OS 的 Vibe-Trading 通过 HTTP 访问该 Bridge。交易类接口需要 `X-API-Key`。

### 使用入口

- 选择档案：`qmt-live-sdk-readonly`（或 `qmt-paper-sdk`）
- Agent：`trading_connections` / `trading_select_connection` / `trading_account` / `trading_positions` / `trading_orders` / `trading_quote` / `trading_history`
- CLI：`vibe-trading connector account|positions|orders|quote|history`
- Runtime：`/live/status?broker=qmt`

## 明确不做

- 下单、改单、撤单
- OAuth / mandate 实盘通道（仍仅 IBKR、Robinhood）
- QMT 客户端「交割单」历史 CSV 专用解析（仍走同花顺 / 东财 / 富途 / 通用表头）。Bridge 成交接口只覆盖**当日**，不能替代多日交割单。

## 涉及文件（27）

- 新增：`agent/src/trading/connectors/qmt/`、`agent/tests/test_qmt_connector.py`
- 注册：`env_schema`、`profiles`、`service`、`live/registry`、`.env.example`
- 账户表面：`cli/_legacy.py`、`trading_connector_tool.py`、`mcp_server.py`、`SKILL.md`
- 前端：Runtime 配置提示与 i18n、Composer 组合摘要、Runtime 测试
- 桌面：`secure-credentials.ts`
- 测试：CLI renderer、env schema、paper-capped 分类、sdk connectors
