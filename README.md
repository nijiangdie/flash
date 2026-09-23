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
# 1. 采集（job51 / zhitong 无需登录，开箱可用）
python cli.py collect --platform job51 --query "python" --city 全国 --pages 3
python cli.py collect --platform zhitong --query "python" --city 东莞 --pages 2

# 2. 只有 Boss直聘 要先登录一次（用本机真实 Edge，扫码后按回车验证）
python cli.py login boss --edge
python cli.py collect --platform boss  --query "python" --city 深圳 --pages 5   # 采集时保持 Edge 窗口开着

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
| Boss直聘 | 同源直连 `wapi/zpgeek/search/joblist.json` | 需要（走真实 Edge，见下） | ⚠️ 通道可用，容易触发风控 |
| 前程无忧 | we.51job.com 卡片 sensorsdata 解析 | 不需要 | ✅ 已实测，**日常主力** |
| 智通直聘 | 搜索页服务端渲染 HTML，直连解析条目 `data-*` 字段 | 不需要 | ✅ 已实测，**连浏览器都不用开** |

**智通直聘（2026-09-23 实测）**：`/s/search/?keyword=&city=&pn=` 返回的就是渲染好的 HTML，每条职位是
`<li class="position pos-item PosName" data-...>`，标题/公司/薪资/镇区/学历/经验/福利全在 `data-*` 属性里，
所以用最普通的 HTTP 请求就能采，不用登录也不用开浏览器（实测 30 条/页，镇区与公司零缺失）。
站点前面有道 WAF 会先发 `CT6T` cookie 再 302 一次，带 cookie jar 自动重跟即可。翻页参数是 `pn`
（第 2 页与第 1 页 0 重复）。旧实现之所以"实验性"，是因为它选了 `.job-card`——那是**首页**卡片的类名前缀，
结果页用的是 `pos-item`，所以永远选空。

**Boss直聘 为什么单独一套流程（2026-09-19 实测）**：Playwright 自建的浏览器能打开 zhipin.com，
但十几秒后会被平台的安全脚本清成 `about:blank`；去掉自动化标记、换 Edge 二进制、独立用户目录都躲不掉。
改成**用本机真实 Edge 打开页面、事后用 CDP 接管**（`--edge`），实测接管后页面不再被清空。

```bash
python cli.py login boss --edge     # 弹出真实 Edge，扫码登录，回命令行按回车检查登录凭证
```

登录判据是 cookie 里的 `bst` / `zp_at`（匿名访问只有 `__a`/`__c`/`__g` 之类），检查用的是浏览器级
API，不依赖任何标签页——Boss 的 SPA 跳转会把接管的标签页换掉，靠 Page 对象会崩。
采集同样不开页面、不监听响应（响应体在 CDP 下经常取不到），而是用同一个登录上下文
直接 `ctx.request.get` 调搜索接口翻页。

**采集前那个 Edge 窗口必须还开着**。窗口关了会直接提示你重跑一次 `login boss --edge`
（profile 里已有 cookie，弹出来后按回车即通过），不会再偷偷退回自建浏览器——那条死路只会
换回一个 `code=37`，还让人误判成"风控了"。
这条通道专用的浏览器配置刻意放在项目外：`%LOCALAPPDATA%\job-stats\edge-profile`。
Edge 会往任何用户目录里塞自带组件和缓存（实测 100M+），放项目外免得拖大工程体积；
登录状态在里面，勿外传。

**风控实测**：Edge 通道下单页请求正常返回 15 条；连续多页/短时间内反复请求会返回
`code=37 您的环境存在异常`。遇到就停手，隔天再跑，一次一页足够。

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
│       └── zhitong.py      # 智通直聘采集器（纯 HTTP，不用浏览器）
├── tests/                  # 单元测试：python -m unittest discover -s tests
├── data/                   # 数据库 + 登录态（自动生成，勿外传；Boss 的浏览器配置在 %LOCALAPPDATA%）
└── reports/                # 生成的 HTML
```

`serve` 只绑定 `127.0.0.1`，页面接口带一次性 token（每次启动重新生成），
用于挡住本机其它进程/网页的跨站请求；采集接口会真实驱动浏览器，不要转发或反代该端口。

Boss 的 Edge 通道同理只监听 `127.0.0.1:9223`，那是那个窗口的调试端口，本机任何进程都能驱动它——
用完关掉窗口即可，同样不要转发或反代。

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
