# 职位统计工具（job-stats）

多平台职位采集 + 本地统计分析。覆盖 **Boss直聘 / 前程无忧 / 智通直聘**。

## ⚠️ 合规使用须知（先读这个）

- 本工具**只采集公开职位信息**（职位名/薪资/公司/城市/技能标签），**不采集**任何简历、联系方式等个人信息
- 内置限速：每次翻页随机间隔 3~8 秒；每日采集总量熔断 200 条（`jobstats/config.py` 中可查）
- 数据**只存你本地** SQLite，不上传、不分发；请勿用于商业再分发
- 如遇平台验证码/风控提示，**立即停止**，隔天再试。请勿调高频率、请勿多开

## 安装

```bash
# 使用已有 venv（已装好 playwright 可跳过）
pip install playwright
playwright install chromium
```

## 使用

**日常只用一条命令**：双击 `start.bat`，或

```bash
python cli.py serve
```

浏览器会自动打开 `http://127.0.0.1:8765`，**采集和筛选都在这个页面里完成**
（选平台/城市/关键词 → 开始采集 → 完成后表格自动刷新）。Ctrl+C 停止。

命令行方式（不需要界面时用）：

```bash
# 1. 首次使用：登录 Boss直聘（会打开浏览器，扫码后自动保存登录态）
python cli.py login boss

# 2. 采集（job51 / zhitong 无需登录）
python cli.py collect --platform boss  --query "python" --city 深圳 --pages 5
python cli.py collect --platform job51 --query "python" --city 全国 --pages 3
python cli.py collect --platform zhitong --query "python" --city 东莞   # 实验性

# 3. 看报告（浏览器打开生成的 HTML）
python cli.py report                     # → reports/report.html

# 4. 静态筛选台：不依赖本地服务，双击 HTML 即可筛选（但不能在页面里采集）
python cli.py browse                     # → reports/jobs-browser.html

# 附：用演示数据预览样式
python cli.py report --demo
python cli.py browse --demo

# 附：导出 CSV
python cli.py export
```

一次采集建议一天跑 1~2 个关键词即可，多跑几天数据自然积累。

## 各平台说明

| 平台 | 方式 | 登录 | 成熟度 |
|---|---|---|---|
| Boss直聘 | 搜索页 wapi JSON 拦截 | 需要（扫码一次，登录态可复用） | ✅ 主力 |
| 前程无忧 | we.51job.com 卡片 sensorsdata 解析 | 不需要 | ✅ 已实测 |
| 智通直聘 | XHR 通用嗅探 + DOM 兜底 | 不需要 | ⚠️ 实验性，接口不稳定 |

## 目录结构

```
job-stats/
├── cli.py                  # 命令行入口
├── start.bat               # Windows 一键启动 serve
├── jobstats/
│   ├── config.py           # 限速/额度等合规参数 + 字段白名单
│   ├── db.py               # SQLite 存储 + 薪资解析 + CSV 导出
│   ├── browser.py          # Playwright 会话/登录态/限速/每日额度
│   ├── report.py           # 统计报告 + 筛选台模板（静态版与服务版共用一套 UI）
│   ├── server.py           # 本地 Web 服务（页面内采集，仅监听 127.0.0.1）
│   └── collectors/
│       ├── boss.py         # Boss直聘采集器
│       ├── job51.py        # 前程无忧采集器
│       └── zhitong.py      # 智通直聘采集器（实验性）
├── tests/                  # 单元测试：python -m unittest discover -s tests
├── data/                   # 数据库 + 登录态（自动生成，勿外传）
└── reports/                # 生成的 HTML
```

`serve` 只绑定 `127.0.0.1`，页面接口带一次性 token（每次启动重新生成），
用于挡住本机其它进程/网页的跨站请求；采集接口会真实驱动浏览器，不要转发或反代该端口。

## 测试

```bash
python -m unittest discover -s tests        # 核心层：薪资解析/入库去重/额度/HTML 输出加固
```

## 修改界面

筛选台的 HTML/JS 只有一处：`jobstats/report.py` 的 `FILTER_TEMPLATE`。
静态版与 `serve` 版共用它，`serve` 额外注入 `WEB_COLLECT_PANEL`（采集表单）
和 `WEB_COLLECT_SCRIPT`（任务轮询）。改完跑一次测试，再 `python cli.py serve` 看效果。

## 统计维度

薪资分布直方图、各平台职位量与薪资中位数对比、城市 Top、技能需求 Top、
学历/经验分布、福利标签、最近职位明细表。
