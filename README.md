# A股大盘每日战报 - 云端推送方案

## 架构总览

```
你的电脑（关机也没关系）
         ↓ 完全无关
GitHub服务器（24小时在线）
  ├── 定时触发器：到点自动唤醒（UTC 00:30/06:30/07:30）
  ├── 启动临时Linux虚拟机（Ubuntu）
  ├── 从仓库下载代码
  ├── 安装Python依赖（akshare/requests/pandas）
  ├── 运行脚本：从东方财富/新浪拉取A股大盘行情
  ├── 计算技术指标（MA20/60、支撑压力位）
  ├── 生成战报内容（三种时段）
  └── 通过Webhook推送到飞书群
         ↓
   飞书群收到交互卡片消息
```

## 双轨架构

| 通道 | 解决什么问题 | 触发方式 | 优势 |
|------|------------|---------|------|
| 本地Windows任务 | 电脑开机/睡眠时推送 + 云文档归档 | 定时唤醒 + 开机补跑 | 可归档云文档、可执行复杂模型 |
| GitHub云端 | 电脑完全关机时推送 | GitHub Actions定时触发 | 24小时在线、不依赖本地电脑 |

两个通道同时运行，互为备份。电脑关机时云端顶上，电脑开机后本地自动同步归档云文档。

## 文件结构

```
cloud/
├── a_share_daily_push.py          # 主推送脚本（采集+生成+发送）
├── requirements.txt                # Python依赖
├── README.md                       # 本说明文档
└── .github/
    └── workflows/
        └── daily_push.yml          # GitHub Actions工作流
```

## 三种战报类型

| 时段 | 北京时间 | UTC时间 | 内容 |
|------|---------|---------|------|
| 开盘前预期 | 08:30 | 00:30 | 基于昨日收盘的今日预判、关键价位 |
| 盘中战报 | 14:30 | 06:30 | 盘中实时数据、板块异动、尾盘观察 |
| 收盘总结 | 15:30 | 07:30 | 完整收盘数据、技术面、明日观察 |

## 部署步骤

### 第一步：创建飞书自定义机器人

1. 打开飞书，进入目标群聊
2. 点击群设置 → 群机器人 → 添加机器人 → 自定义机器人
3. 设置机器人名称（如"A股大盘战报"）和头像
4. 安全设置：建议选择"签名校验"或"IP白名单"（GitHub Actions的IP不固定，建议用签名校验）
5. 复制Webhook地址，格式如：`https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`

### 第二步：创建GitHub仓库

1. 登录GitHub，创建新仓库（建议Private）
2. 仓库名建议：`a-share-daily-push`
3. 将 `cloud/` 目录下的所有文件上传到仓库根目录
   - `a_share_daily_push.py`
   - `requirements.txt`
   - `.github/workflows/daily_push.yml`

### 第三步：配置GitHub Secrets

1. 进入GitHub仓库 → Settings → Secrets and variables → Actions
2. 点击 "New repository secret"
3. Name: `FEISHU_WEBHOOK`
4. Secret: 粘贴第一步复制的飞书Webhook地址
5. 点击 "Add secret"

### 第四步：测试工作流

1. 进入GitHub仓库 → Actions
2. 左侧选择 "A股大盘每日战报推送"
3. 点击 "Run workflow"
4. 选择推送时段（morning/midday/close）
5. 点击 "Run workflow"
6. 等待1-2分钟，查看飞书群是否收到战报消息

### 第五步：确认定时触发

工作流已配置三个定时触发器（仅工作日周一至周五）：
- UTC 00:30 = 北京时间 08:30（开盘前预期）
- UTC 06:30 = 北京时间 14:30（盘中战报）
- UTC 07:30 = 北京时间 15:30（收盘总结）

> ⚠️ 注意：GitHub Actions的定时触发可能有5-15分钟的延迟，这是正常现象。

## 本地增强（可选）

如果希望电脑开机时也能推送并归档云文档，可以配合本地Windows定时任务：

1. 创建Windows定时任务，触发时间同为08:30/14:30/15:30
2. 设置 `WakeToRun=True`（睡眠时自动唤醒）
3. 设置 `StartWhenAvailable=True`（关机错过后开机补跑）
4. 本地任务执行完整的模型计算+云文档归档+飞书推送

PowerShell命令示例：
```powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "C:\path\to\a_share_daily_push.py --period close"
$trigger = New-ScheduledTaskTrigger -Daily -At 15:30
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable
Register-ScheduledTask -TaskName "A股大盘_收盘战报" -Action $action -Trigger $trigger -Settings $settings
```

## 数据源说明

| 数据 | 数据源 | 备用源 |
|------|--------|--------|
| 指数行情（上证/深证/创业板/科创50/沪深300） | AKShare | 新浪财经API |
| 市场宽度（涨跌家数/成交额） | AKShare | 东方财富 |
| 板块涨跌幅排行 | AKShare | 东方财富 |
| 技术指标（MA/支撑压力位） | 本地计算 | - |

所有数据源均为公开免费接口，无需付费。

## 战报卡片格式

参考588780芯片ETF每日战报风格，使用飞书交互卡片：

```
📊 A股大盘 每日战报 | 日期 · 收盘总结
「警戒 Alert」
[上证指数] [日涨跌幅] [两市成交]  ← 核心指标
🎯 市场判断
📍 关键价位（第一支撑/强支撑/压力位）
📊 技术面（MA20/MA60状态）
🏭 板块异动（领涨/领跌）
📝 明日观察
⚠️ 风险提示
```

## 常见问题

### Q: GitHub Actions定时触发不准时？
A: GitHub的定时触发器有5-15分钟延迟是正常现象。如果需要精确到分钟，建议使用本地Windows定时任务作为主通道，GitHub作为备份。

### Q: 飞书机器人收不到消息？
A: 检查以下几点：
1. Webhook地址是否正确配置到GitHub Secrets
2. 飞书机器人是否被移出群聊
3. GitHub Actions运行日志是否有报错
4. 飞书机器人安全设置是否拦截了消息

### Q: 数据采集失败怎么办？
A: 脚本内置了备用数据源（新浪财经API），如果AKShare失败会自动切换。如果两个数据源都失败，战报会显示"待补充"，不会编造数据。

### Q: 如何修改推送时间？
A: 编辑 `.github/workflows/daily_push.yml` 中的cron表达式。注意GitHub使用UTC时间，北京时间=UTC+8。

### Q: 如何添加更多指数或板块？
A: 编辑 `a_share_daily_push.py` 中的 `fetch_index_data()` 和 `fetch_sector_performance()` 函数，添加更多代码即可。

## 成本说明

- GitHub Actions：免费额度每月2000分钟（Private仓库），本方案每天运行3次×约2分钟=每月约130分钟，完全在免费额度内
- 飞书自定义机器人：免费
- 数据源：全部免费公开接口

**总成本：0元/月**

## 风险提示

本战报仅用于研究与模型校准，不构成投资建议。股市有风险，投资需谨慎。
