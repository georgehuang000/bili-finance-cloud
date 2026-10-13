# B站UP主投资内容监控

定时爬取指定 B 站投资类 UP 主的最新视频与动态，通过 LLM 生成每日投资内容日报，并提供 Web 界面查看。

## 架构

```
B站 Web API (纯 httpx 直调, 默认)  →  server (FastAPI 后端 + SQLite + APScheduler)  →  web/dist (React 构建产物)
                │                                    ↓
                │  USE_PURE_API=false 时回退          OpenAI兼容API（默认DeepSeek官方）生成日报
                └── external/MediaCrawler (无头浏览器子进程)
```

- **爬取**：默认纯 API 模式（`bili_api.py` + `anti_spider.py`，含 buvid/bili_ticket/WBI 签名等反风控），无需浏览器，需要 `.env` 配置 B 站 Cookie；`USE_PURE_API=false` 时回退 MediaCrawler 子进程（仅本地使用，服务器上不可用）。
- **总结**：汇总近 24 小时新增视频/动态素材，调用 `deepseek-v4-pro`（失败回退 `deepseek-v4-flash`）生成 Markdown 日报。
- **前端**：React + Vite 构建到 `web/dist/`，由 FastAPI 同进程托管（单端口 9000），LLM 输出经 `SafeMarkdown` 组件安全渲染。

## 目录说明

| 目录 | 说明 |
| --- | --- |
| `server/` | **当前使用的后端**（FastAPI + SQLite + APScheduler） |
| `web/` | **当前使用的前端**（React + Vite，dev 端口 5173，代理 `/api` 到 9000） |
| `external/MediaCrawler/` | 爬虫引擎（独立 venv，登录态缓存于 `browser_data/bili_user_data_dir`） |
| `backend/`、`frontend/` | **已废弃的旧实现，请勿启动**——新旧版本同样使用 9000/5173 端口，同时启动会端口冲突 |

## 启动步骤（Windows 本地）

### 一键启动（推荐）

双击 **`start.bat`** 即可，脚本会自动：

1. 检查并补装缺失的 Python 依赖（venv 不存在则创建，`requirements.txt` 缺包则补装）
2. **前端源码比 `web/dist` 新时自动重建**，避免网页显示旧界面
3. 端口已被本服务占用时直接复用，不会重复启动
4. 启动后端 → 等健康检查通过 → 打开浏览器，日志写入 `logs\backend.log`

浏览器访问 http://127.0.0.1:9000（前端产物由后端同端口托管，无需另起 dev server）。

```powershell
start.bat                 # 正常启动（双击亦可）
start.bat -Restart        # 先停掉已在运行的本服务再启动（改完代码用这个）
start.bat -NoBuild        # 跳过前端重建检查（只动后端时更快）
start.bat -NoBrowser      # 不自动打开浏览器
start.bat -Foreground     # 前台运行，日志直接打在窗口里（Ctrl+C 停止）
start.bat -Bind 0.0.0.0   # 监听所有网卡（局域网访问；注意本项目无鉴权）
stop.bat                  # 停止服务（连子进程一起清理，反复启停不会堆积进程）
```

> **改脚本时注意编码**：PowerShell 5.1 读无 BOM 的 UTF-8 会把中文读成乱码，所以
> `start.ps1` / `stop.ps1` 必须保存为 **UTF-8 with BOM**；`.bat` 只放纯 ASCII
> （cmd 按 GBK 解析，中文同样会乱）。

### 手动启动（等价命令）

```powershell
cd server
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 9000
```

只有改了前端才需要 `cd web; npm run build`（启动脚本会自动完成这一步）。

开发模式（前端热更新，端口 5173）：

```powershell
cd web
npm run dev
```

### B站登录态

爬取必须有 B站登录态，配置在根目录 `.env` 的 `BILI_SESSDATA` / `BILI_JCT` / `BILI_BUVID3` /
`BILI_BUVID4` / `BILI_DEDEUSERID`。最省事的方式是启动后在网页顶栏点 **「扫码登录」**，
用 B站 App 扫一下即可（登录态自动写回 `.env` 并即时生效，无需重启）。
失效判断与完整排查见下方「风控与重新登录 SOP」。

## 服务器部署（Linux，推荐方式）

生产环境只需一个进程：FastAPI 同时提供 API、定时爬取和前端静态页面，访问 `http://服务器IP:9000`。

### 1. 上传代码

整包上传到服务器（如 `/opt/bili-monitor`），**可排除**以下目录以大幅减小体积：
`external/`（纯 API 模式完全用不到，约 1GB+）、`web/node_modules/`、`backend/`、`frontend/`、`server/tmp/`。

需要上传的关键内容：`server/`（不含 `.venv`）、`web/dist/`（已构建好的前端，服务器无需 Node）、`.env`、`deploy/`、`README.md`。

### 2. 初始化并启动

```bash
cd /opt/bili-monitor
bash deploy/setup_server.sh          # 建 venv + 装依赖
sudo cp deploy/bili-monitor.service /etc/systemd/system/   # 路径不同先改 service 文件里的两处路径
sudo systemctl daemon-reload
sudo systemctl enable --now bili-monitor
```

云服务器控制台安全组 / 防火墙放行 **9000/TCP**，浏览器访问 `http://服务器IP:9000`。

### 3. 上线前必须检查

- **`.env` 里 B 站登录态必须先配好**（`BILI_SESSDATA` / `BILI_JCT` / `BILI_BUVID3` / `BILI_DEDEUSERID`）。
  最省事的方式是部署完**打开网页点「扫码登录」**用 B站 App 扫一下（自动写入 `.env` 并热生效），
  也可从浏览器 F12 → Application → Cookies → bilibili.com 复制。机房 IP 匿名请求几乎必被风控。
- `AUTO_CRAWL_ON_START=true`（已默认开启）：服务每次启动/重启会自动补跑一轮「爬取 + 总结」，宕机错过的定时任务靠它兜底。
- 纯 API 模式是服务器上唯一可用的爬取方式（MediaCrawler 回退方案需要 Chrome 图形环境，无头服务器不可用）。

### 4. 运维速查

```bash
journalctl -u bili-monitor -f                 # 实时日志（应用日志已开 INFO，爬取/enrich 轨迹可见）
sudo systemctl restart bili-monitor           # 重启（会自动补跑一轮）
curl -s http://127.0.0.1:9000/api/status      # 本机健康检查
curl -s http://127.0.0.1:9000/api/diagnostics # 一站式诊断快照（排错先看这个）
sqlite3 server/data.db 'select count(*) from videos;'   # 数据量
```

- **排错先看 `/api/diagnostics`**：一次返回登录态（含最近检测时间与缓存内容）、`.env` 里哪些 Cookie
  配了/没配（只报有无，不泄露值）、风控计数（哪个接口、什么错误码、几次、冷却剩余）、
  每个 UP 主最近一轮结果、逐视频 enrich 统计、库内数据量与字幕/摘要覆盖率。
- 应用日志级别由 `.env` 的 `LOG_LEVEL`（默认 `INFO`）控制；此前未配置 logging 导致 `logger.info`
  全部丢失（journalctl 里只有 warning），现已修复。
- 必须单进程运行（service 文件默认如此）：爬取互斥锁、APScheduler 都在进程内存里，多 worker 会重复爬取。
- 日报生成于每轮爬取之后；换 LLM key 推荐用网页顶栏的「模型设置」（写回 `.env` 并热生效，
  无需重启），手动改 `.env` 后则需要 `systemctl restart`。

## .env 配置项（工作区根目录 `.env`）

> LLM 相关配置推荐在网页顶栏点「模型设置」修改：选服务商预设、粘贴 Key、点「测试连接」，
> 保存后自动写回本文件并**即时生效**（无需重启）。下表供手动配置或排错时参考。

| 配置项 | 说明 |
| --- | --- |
| `LLM_API_KEY` | DeepSeek 官方 API Key（**保密，勿提交/打印**）；网页端保存时写这个键 |
| `deepseek_key` | 等价的手动键名（优先级低于 `LLM_API_KEY`），不习惯用上面那个就写这个 |
| `LLM_BASE_URL` | LLM 接口地址，默认 `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 主模型，默认 `deepseek-flash`（= DeepSeek-V4.1-Flash，支持图片识别）；**不做兜底模型** |
| `VISION_MODEL` | 图片识别用模型，留空则跟随 `LLM_MODEL` |
| `LLM_THINKING` | 思考模式：`disabled`（默认）/ `low` / `high` / `max` |
| `VISION_ENABLED` | 是否识别封面/配图，默认 `true`（需服务商支持读图） |
| `VISION_MAX_IMAGES_PER_ROUND` | 每轮最多识别几张图，默认 `20` |
| `UP_UIDS` | 逗号分隔的 UP 主 UID 列表（**仅首次启动引导初始化订阅名单用**，之后的增删请在网页端「监控名单」操作） |
| `CRAWL_MORNING` / `CRAWL_EVENING` | 定时任务时间，默认 `08:00` / `18:00` |
| `CRAWL_SLEEP_MIN` / `CRAWL_SLEEP_MAX` | 每轮爬取中相邻 UID 的请求间隔秒数，默认 `12` / `20`；风控频繁时可调大 |
| `LOG_LEVEL` | 应用日志级别，默认 `INFO`（排错时可改 `DEBUG`） |
| `AUTO_CRAWL_ON_START` | 后端启动时是否立即触发一轮爬取，默认 `false` |
| `SUMMARY_INPUT_MAX_CHARS` | 日报素材上限，默认 `200000` 字（deepseek-flash 有 1M 上下文，别设太小，否则尾部 UP 的内容会被静默截断） |
| `SUMMARY_SUBTITLE_FALLBACK_CHARS` | 视频还没有 AI 摘要时，用字幕开头这么多字兜底，默认 `1200` |

## 自动观点研究

在顶栏打开“观点研究”，可切换全部或单个 UP 主、筛选主题，查看自动提取的观点、原文和历史变化。每轮采集后自动分析新或变更素材，已有结果去重复用；也可点击“分析已有素材”补处理历史内容。

研究核验页会自动列出待核验问题。补充支持的官方公告或财报 HTTPS 链接后，系统读取网页或文字层 PDF 并核验，保留来源、原文和信息缺口。它不会自动把 UP 主的说法当成已证实事实。

树莓派页面为 `http://100.96.226.36:9000/research`，电脑需连接同一个 Tailscale 网络。若浏览器直连受代理影响，在项目目录运行 `tools/research-tunnel.ps1`，使用 `http://127.0.0.1:19001/research`。详细流程、限制和验收见 [自动分析接入记录](docs/自动分析接入记录.md)。

## 日报（每日投资内容汇总）

- **素材来源**：已订阅 UP 主在**近 24 小时**内的全部新视频与新动态。视频传
  标题 + 简介 + AI字幕摘要（没有摘要时用字幕开头一段兜底）+ 封面识别结果；
  动态传文字 + 配图识别结果。窗口内不设条数上限。
- **日报结构**（刻意设计成"要求横向对照"而不是"逐条复述"，这样模型才有推理空间）：
  1. 今日主线（核心矛盾 / 情绪面 / 资金面）
  2. 共识 —— 只收被 2 个以上来源支撑的判断，每条附依据与链接
  3. 分歧与对立 —— 写成「甲认为…｜乙认为…｜分歧点在于…」，找不到就写明没有
  4. 硬数据汇总 —— 表格，只收素材里明确出现的数字，不许推算
  5. 今日新增的论据 —— 区分新论据与老调重弹
  6. 值得跟踪的变量 —— 每条附「若出现 X 信号则判断需推翻/修正」
- **防臆造**：要求严格区分「素材原文观点」与「模型推断」，推断必须标注；
  单一来源、未证实的说法标注「（单一来源，未证实）」。
- **窗口是"近 24 小时"滚动窗口，不是自然日**：早间那轮覆盖昨天早上至今，
  晚间那轮覆盖昨天傍晚至今，两轮之间有重叠、也会各自漏掉一部分——需要严格
  "今天一整天"的话请自行调整 `SUMMARY_LOOKBACK_HOURS`（代码里的常量）。

## 图片识别（读图）

- 视频封面与动态配图会被送去多模态模型读一遍，抽出的文字（板块名、研报标题、数据）
  展示在卡片上，并作为素材喂给日报。整轮爬取末尾跑一次，每轮最多 `VISION_MAX_IMAGES_PER_ROUND` 张。
- 需要服务商支持读图：DeepSeek 官方只有 `deepseek-flash` 支持（`deepseek-v4-pro` 不支持）。
  检测到不支持时会整段跳过，不会白跑失败请求；网页「模型设置」里也有「测试图片识别」可自测。
- **实测结论**：动态配图能拿到，但来源是"绘图类"动态（`DRAW`）里的 `draw.items[].src`，
  而不是图文动态的 `opus.pics`（后者实测常为空，一度让人误以为动态没有配图）。
  2026-09-19 一轮实测：20 张图 = 12 张视频封面 + 7 张动态配图，零失败。
  抽出来的内容相当有用，例如某条动态的行情图被读成
  「海吉亚医疗(06078)，现价9.525港元，跌2.41%，总市值58.91亿，市盈率(TTM)28.10，
  2026年中报经营现金流5.302亿元」——这类数字是字幕里根本没有的。

## 订阅管理（网页端增删 UP 主）

在首页「监控名单」区块顶部的搜索框输入 **UP 主名字**（走 B站搜索，列出候选）或 **纯数字 UID**（直查用户卡片），点「订阅」即加入名单：

- 订阅写入 SQLite `uppers` 表（`subscribed` 标记），**立即在后台抓取该 UP 主一轮**，卡片很快有数据；无需改 `.env`、无需重启。
- 卡片右上角 ✕ 取消订阅：保留已抓取的历史数据，重新订阅即恢复，且重启后不会被 `.env` 引导重新激活。
- 名单以数据库为唯一数据源；爬取与总结每轮开始时实时读取，定时任务无需重启即可生效。
- 相关 API：`GET /api/uppers/search?q=`、`POST /api/uppers`、`DELETE /api/uppers/{uid}`。

## 定时任务

- 每天 **08:00** 与 **18:00**（Asia/Shanghai）自动执行「爬取 → 总结」。
- 依赖后端进程**常驻**：uvicorn 进程退出或机器休眠期间错过的任务**不会补跑**，恢复后等待下一个整点触发（也可手动 POST `/api/crawl`、`/api/summarize`）。
- 爬取与总结各有独立互斥：定时触发时若同类任务已在运行则跳过并记录日志。

## 风控与重新登录 SOP

**判断登录态是否失效**：网页顶栏出现红色「登录态失效」芯片，或 `curl -s http://127.0.0.1:9000/api/login/status` 返回 `logged_in: false`。

> 注意：SESSDATA 失效后，空间 feed 通常返回 **-352（风控）而不是 -101（未登录）**——因为匿名请求风控阈值极低。
> 所以「大面积 -352」往往不是限流，而是登录态掉了。系统为此做了四层防护，**不必等下一轮爬取才发现**：
>
> 1. **每轮爬取前主动检测**（`nav` 接口 `isLogin`）：未登录直接跳过本轮，不再匿名硬打接口。
> 2. **后台定期复查**：网页轮询 `/api/status` 时，若距上次检测超过 5 分钟，会在后台线程复查一次并更新状态
>    （最长 5 分钟内网页就会显示「登录态失效」）。
> 3. **报错交叉验证**：feed 报 `-352` 时会顺带验一次登录态；若实为未登录，`last_error` 记为
>    `login_required` 而不是 `risk_control(-352)`——避免把「没登录」误判成「被限流」而白等半天。
> 4. **可观测**：`/api/diagnostics` 返回登录态、最近检测时间、Cookie 配置情况与风控计数；
>    前端总览页有「重新检测登录态」按钮。

### 方式 A：网页扫码登录（推荐，无需重启）

1. 打开网页，点顶栏 **「扫码登录」**（登录态失效时该按钮为高亮态）。
2. 用手机 B站 App「扫一扫」扫描弹窗中的二维码，并在手机上确认。
3. 登录成功后，`SESSDATA / bili_jct / DedeUserID / buvid3 / buvid4` 会自动写入工作区根目录 `.env`，
   并**即时热生效**（无需重启后端），弹窗提示「登录成功」。

相关接口：`POST /api/login/qrcode`（生成二维码）、`GET /api/login/qrcode/poll?key=`（轮询）、`GET /api/login/status`（检测登录态）。

> ⚠️ 安全提示：本项目本身没有鉴权体系（任何能访问 9000 端口的人都能触发爬取、增删订阅）。
> 扫码登录接口同样未鉴权——能访问该端口的人可以用**自己的** B站账号登录进来，从而接管爬取身份。
> 若部署在公网，请用云安全组/防火墙限制来源 IP，或置于带鉴权的反向代理之后。

### 方式 B：手动复制 Cookie

浏览器 F12 → Application → Cookies → `bilibili.com`，复制 `SESSDATA` / `bili_jct` / `buvid3` / `buvid4` / `DedeUserID`
填入 `.env` 的 `BILI_SESSDATA` / `BILI_JCT` / `BILI_BUVID3` / `BILI_BUVID4` / `BILI_DEDEUSERID`，然后重启后端。

### 方式 C：MediaCrawler 浏览器扫码（旧方式，仅本地）

纯 API 模式用不到（它读 `.env` 而非浏览器 profile），仅在需要修复 MediaCrawler 回退方案时使用：

1. 停掉后端（避免定时任务与手动登录抢占浏览器数据目录）。
2. 将 `external/MediaCrawler/config/base_config.py` 中 `HEADLESS = True` 临时改回 `HEADLESS = False`。
3. 在 `external/MediaCrawler` 目录下用其 venv 跑一次爬取，弹出浏览器后扫码登录：
   ```powershell
   cd external\MediaCrawler
   .\.venv\Scripts\python.exe main.py --platform bili --type creator --lt qrcode --save_data_option json --get_comment no
   ```
4. 登录成功后改回 `HEADLESS = True`，重启后端。

遇到 412 风控：等待一段时间（数十分钟到数小时）再试，勿高频重试；系统对单个 UID 的失败只记录 `last_error` 并继续本轮其余 UID。

## 已知限制

- **空间 feed 接口易 -352 风控**：纯 API 模式下视频与动态都取自同一个空间动态 feed 接口
  （投稿接口 `/x/space/wbi/arc/search` 已被 B站 `w_webid` 封锁），该接口在连续请求时会返回
  `-352`，有时还会返回 **HTTP 200 + code=0 但 items 为空**的「软限流空包」。
  代码已对这两类情况做退避重试（`-352/-412/-799` 三次退避、空包两次退避）并在退避用尽后
  置一段冷却；仍失败则记入 `last_error`，网页端卡片会显示「风控限流 / 软限流空包」角标。
  风控是账号/IP 维度的限流，**频繁手动点「立即爬取」会延长风控时间**，建议间隔数十分钟再试。
  间隔可通过 `.env` 的 `CRAWL_SLEEP_MIN` / `CRAWL_SLEEP_MAX`（默认 12/20 秒）调大。
- **添加 UP 主：UID 直查比关键词搜索可靠**。关键词搜索走 wbi 搜索接口，在未登录/风控状态下会
  **间歇性返回空包**（HTTP 200、code=0 但缺失 `result`、`numResults` 为 None，实测约半数请求如此）。
  代码已对空包做短退避重试，并区分三种返回：`result` 是列表 = 正常；
  `numResults=0` = 确实没搜到（返回空列表）；两者都不是 = 限流空包（重试后报错提示"稍后重试或改用 UID"）。
  若搜索不出结果，直接输入 UID 搜索即可，`card` 接口稳定得多。
- **MediaCrawler 输出脱敏**：其输出中创作者标识为匿名 hash，无法从输出反查 UID，故本系统以**配置文件中的 UID 为主键**、每次子进程只跑一个 UID 来做归属。
- 单 UID 单模式爬取超时 5 分钟，超时会强杀整棵进程树（含 Chrome 子进程）。
- `server/tmp/` 下的中间 JSON 每轮爬取前自动清理 7 天以前的文件。
# 云端测试版本

公网权限、Vercel/PostgreSQL/GitHub Actions 接入流程与测试记录见 [docs/云端部署与测试记录.md](docs/云端部署与测试记录.md)。云端使用 DeepSeek 官方 API，初始只允许手动少量测试，自动采集保持关闭。原树莓派服务保留至全云链路验收完成。

