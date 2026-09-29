# 职位统计工具（job-stats）

多平台职位采集 + 本地统计分析。覆盖 **Boss直聘 / 前程无忧 / 智通直聘**。

## ⚠️ 合规使用须知（先读这个）

- 本工具**只采集公开职位信息**（职位名/薪资/公司/城市/技能标签），**不采集**任何简历、联系方式等个人信息
- 内置限速：每次翻页随机间隔 3~8 秒；每日采集总量熔断 200 条（`jobstats/config.py` 中可查）
- 职位正文（JD）要逐条打开详情页，额度单独算且更小：**每天 20 条**（`DETAIL_DAILY_CAP`）
- 投递状态/备注（`status`/`note`）是**你在这台机器上自己标的**，不来自平台，也不会拿你的
  登录状态去平台查"HR 有没有已读"这类私域数据
- 数据**只存你本地** SQLite，不上传、不分发；请勿用于商业再分发
- 如遇平台验证码/风控提示，**立即停止**，隔天再试。请勿调高频率、请勿多开

## 安装

**只需要双击 `install.bat`**（安装逻辑在 `install.py`，批处理只负责找到 Python 并启动它）。
它会检查 Python 版本 → 在项目里建一个独立的 `.venv` → 装 playwright → 下载采集用的
浏览器内核 → 最后跑一遍离线自检测试（不联网、不采集）。装完以后不用再碰它，中途网络断了
就重新双击一次，已完成的步骤会自动跳过。

手工装（不用 `.venv`，直接系统 Python）等价于：

```bash
pip install -r requirements.txt
playwright install chromium
```

### 拷给别人跑（Windows）

把项目文件夹整个拷过去（或压缩发过去），对方只需要两步：

1. 装 Python 3.10 或更高（代码用了 `str | None` 这类新语法，3.9 导入就报错），
   官网安装页面 `https://www.python.org/downloads/`，安装时勾选 *Add python.exe to PATH*
2. 双击 `install.bat`，跑完再双击 `start.bat`

依赖只有一个 `playwright`，数据库用 Python 自带的 SQLite，都不用另装。
Boss 那家用的是对方自己电脑上的 Edge，不需要下载；智通直聘纯 HTTP，连浏览器都不开。

**别把 `data/` 拷给别人**：里面是 `jobs.db`（你的采集记录）和 `data/auth/*`
（你的平台登录 cookie，等于你的账号登录态）。要给的只有代码：`cli.py`、`install.bat`、
`install.py`、`start.bat`、`README.md`、`requirements.txt`、`jobstats/`、`tests/`。对方首次运行会自己
生成空库。`reports/` 是生成产物，也不用给。Boss 那份浏览器配置在项目外
（`%LOCALAPPDATA%\job-stats\edge-profile`），跟着文件夹拷不过去，对方要采 Boss 得自己扫一次码。

`.venv` 是**按机器**生成的（里面写死了绝对路径），换机器就删掉它重新双击 `install.bat`；
浏览器内核默认装在 `%LOCALAPPDATA%\ms-playwright`，同一台机器上多个副本共用一份，不会重复下载。

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

# 附：导出 CSV（Excel 可直接打开）
python cli.py export
python cli.py export --out 我的投递清单.csv

# 附：给要投的那几条抓职位正文（JD），逐条打开详情页
python cli.py detail --ids 562,560              # 库里 id（CSV 第一列 platform_job_id 对应的那条）
python cli.py detail --platform zhitong --limit 5   # 自动挑还没正文的职位补抓
```

一次采集建议一天跑 1~2 个关键词即可，多跑几天数据自然积累。

导出的 22 列里，除了平台原文的 `salary_raw`，还给了 `salary_min / salary_max /
salary_months / salary_annual_wan`（年薪估算，万元/年：月薪中位数 × 发薪月数，
平台没写月数按 12 薪算），以及 `skills / welfare`（已按下节统一口径，` / ` 分隔），
在 Excel 里可以直接按年薪或技能排序筛选。`description` 是职位正文，只有抓过的
那条才有值（见下节）；`status / note` 是你自己标的投递进度（见「投递状态」一节）。

## 职位正文（JD）

统计口径只能告诉你"市场给多少钱"，投递时要看具体那条 JD 写了什么要求。所以正文
单独一张表 `job_details`，**默认不去抓**——逐条打开详情页比翻列表页容易触发风控得多。

- **每日额度单独算**：`DETAIL_DAILY_CAP = 20` 条/天，抓满就熔断，和列表额度互不影响。
  每抓一条照样随机等 3~8 秒。
- **怎么看**：筛选台每行末尾的『抓正文 / 看正文』按钮，点开在这行下方展开；
  或 `python cli.py detail --ids …`；或导出 CSV 看 `description` 列。
- **哪些平台抓得到**（2026-09-27 实测）：
  | 平台 | 正文 | 说明 |
  |---|---|---|
  | 智通直聘 | ✅ 纯 HTTP | 正文在 `<pre class="pos-desc">` 里原样带换行，连浏览器都不用开 |
  | Boss直聘 | ✅ 走 Edge 通道 | 正文在 `<div class="job-sec-text">`，用登录上下文直接 GET 详情页即可，不开标签页 |
  | 前程无忧 | ❌ 不抓 | 详情页 `jobs.51job.com/...` 直接弹滑块验证，按红线立刻停手，代码里一个请求都不发 |
- **只存公开内容**：正文和详情页关键词（`keyword_tags`）进白名单
  `ALLOWED_DETAIL_FIELDS`，HR 姓名/头像/联系方式那一段压根不解析。
- 同一职位重复抓会**覆盖**旧正文（招聘方会改 JD）；页面按钮只在没有正文时才发请求，
  想重抓用 `python cli.py detail --ids … --force`。
- 详情页关键词比列表页更全（智通列表页技能列常是空的），但只存在 `job_details` 里，
  不会自动并进 `jobs.skills`，统计口径仍以列表页为准。

## 投递状态（本地标注）

采回来 180 条之后，真正的问题变成"我投了哪几条、哪条约了面"。所以 `jobs` 表另有两列
`status`（想投 / 已投 / HR已读 / 约面 / 已面 / offer / 不合适）和 `note`（备注，200 字以内）。

- **怎么用**：筛选台每行的『状态』下拉选一下，旁边备注框写一句（**失焦或回车才存**，
  框子绿一下边框就是存好了）。筛选条上的『状态』下拉里选**未标注**，就是"还有哪些没处理"，
  用它挑下一批要投的。
- **它不访问任何平台**：这两列记录的是你自己做过的动作。刻意**不**去做"从 Boss 同步
  HR 是否已读"那种功能——那要用你的登录会话去读私域沟通数据，超出采集红线，也不稳定。
- **采集器写不进这两列**：`status/note` 不在 `ALLOWED_FIELDS` 白名单里，唯一的写入口是
  `db.set_status()`。同一个关键词反复重采也不会把你标过的状态冲掉（入库走的是
  `INSERT OR IGNORE`，已存在的行整行跳过）。
- **老库自动补列**：`init_db()` 会先查 `PRAGMA table_info(jobs)` 再 `ALTER TABLE` 补这两列，
  已有的职位一条不动，老行的状态读出来是空字符串。
- 导出 CSV（`python cli.py export`，22 列）和页面上的『导出当前结果 CSV』都带上这两列，
  在 Excel 里按 `status` 一排序就是一份投递台账。


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

**那个 Edge 窗口不用你管**（`boss.ensure_window()`）：命令行 `collect` 和页面上的『开始采集』都会先
经过它——窗口不在就自己重新弹出来（profile 里已有 cookie，不用再扫码），只有**从没登录过**时才弹
登录页并提示"扫完码再点一次开始采集"。不会再偷偷退回自建浏览器——那条死路只会换回一个
`code=37`，还让人误判成"风控了"。

**每次翻页都重新接管一次 Edge**（`boss.Channel`）。CDP 连接随时会没：窗口被关、被别的 Edge 实例接管、
Edge 自己更新，都会让长期持有的请求上下文变成 `Request context disposed`，一整轮采集跟着报废。
现在断了只重试当前那一页，连续两次接不上才放弃。放弃时只打印报错的**第一行**——Playwright 的
`Call log` 会把请求头原样带出来，里面就有 `bst` / `zp_at` 这两个登录凭证，绝不能落到终端上。
（如果你之前看到过带 cookie 的 traceback，那串凭证当时是明文显示在窗口里的，别把那种日志外发。）

这条通道专用的浏览器配置刻意放在项目外：`%LOCALAPPDATA%\job-stats\edge-profile`。
Edge 会往任何用户目录里塞自带组件和缓存（实测 100M+），放项目外免得拖大工程体积；
登录状态在里面，勿外传。

**风控实测**：Edge 通道下单页请求正常返回 15 条；连续多页/短时间内反复请求会返回
`code=37 您的环境存在异常`。遇到就停手，隔天再跑，一次一页足够。

## 标签口径统一（技能/福利）

三家平台往"技能"这一列塞的东西完全不一样，直接合并统计会得出"第一名是五险一金"这种结论：
智联基本只给福利、前程无忧的标签其实是福利、Boss 连学历（大专/本科）和年限（3-5年）都往里放。

`jobstats/skills.py` 在**读取侧**统一一次：大小写与别名归一（`python`/`PYTHON` → `Python`、
`英文` → `英语`）、同义福利合并（`年终奖`/`年终奖金`、`团建聚餐`/`定期团建`、五险的五个险种
→ `五险`）、混进技能列的福利词挪回福利列、与 `education`/`experience` 重复的标签丢掉。

入库存的仍是平台原文，所以**改规则不用重采**——历史数据下次读出来就跟着变。
要加别名就往 `ALIASES` / `WELFARE_ALIASES` 里补一行。

## 目录结构

```
job-stats/
├── cli.py                  # 命令行入口
├── start.bat               # 双击启动筛选台（优先用 .venv）
├── install.bat             # 双击一键装环境（配合 install.py）
├── install.py              # 安装流程：建 .venv + 装依赖 + 下内核 + 自检
├── jobstats/
│   ├── config.py           # 限速/额度等合规参数 + 字段白名单
│   ├── db.py               # SQLite 存储 + 薪资解析 + 职位正文表 + CSV 导出
│   ├── skills.py           # 技能/福利标签归一（读取侧，入库仍存原文）
│   ├── html_text.py        # 详情页 HTML 片段 → 纯文本（保住换行）
│   ├── details.py          # 逐条抓正文的调度：单独每日额度 + 限速 + 遇验证即停
│   ├── browser.py          # Playwright 会话/登录态/限速/每日额度
│   ├── report.py           # 统计报告 + 筛选台模板（静态版与服务版共用一套 UI）
│   ├── server.py           # 本地 Web 服务（页面内采集/看正文，仅监听 127.0.0.1）
│   └── collectors/
│       ├── boss.py         # Boss直聘采集器（含详情页正文）
│       ├── job51.py        # 前程无忧采集器（详情页要滑块验证，不抓正文）
│       └── zhitong.py      # 智通直聘采集器（纯 HTTP，不用浏览器，含正文）
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
