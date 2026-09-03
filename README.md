<div align="center">

# SunnyRegister

**自托管的 GPT 账号注册、会话管理与支付工作流控制台**

[![CI](https://github.com/pxygit/SunnyRegister/actions/workflows/ci.yml/badge.svg)](https://github.com/pxygit/SunnyRegister/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-10b981.svg)](./LICENSE)
[![Go](https://img.shields.io/badge/Go-1.23%2B-00ADD8?logo=go&logoColor=white)](https://go.dev/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111111)](https://react.dev/)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14%2B-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)

[简体中文](./README.md) | [English](./README_en.md)

[功能概览](#功能概览) · [快速开始](#快速开始) · [使用流程](#建议使用流程) · [部署文档](#部署与运维文档) · [安全策略](./SECURITY.md)

</div>

## 项目简介

SunnyRegister 将邮箱资源、账号注册/登录、手机号验证、代理路由、Session 与 Token、反代导入、Checkout 提链、支付任务和审计日志集中在一个 Web 控制台中。Go 服务负责 API、数据持久化和任务调度，Python Worker 负责协议请求及隔离浏览器自动化，React 前端提供可恢复的任务状态与实时日志。

本项目适用于自有账号、已获授权的资源管理、自动化测试和技术研究。请勿将其用于违反服务条款、侵犯第三方权益或绕过访问控制的场景。

## 功能概览

| 菜单 | 主要能力 |
| --- | --- |
| 工作台 | 批量注册或登录 ChatGPT；选择执行阶段与并发；协议和浏览器流程协同；按邮箱隔离验证码、浏览器上下文和任务日志；任务中断、刷新恢复与阶段检查点 |
| 邮箱配置 | 邮箱批量导入、分组、启停、查信、凭证编辑与导出；支持 Microsoft OAuth、Apple iCloud `xbovo` / `url_api`、Remail 和自建域名邮箱渠道；支持 ChatGPT 密码及 TOTP 凭证 |
| 接码配置 | 自建 `手机号----收码URL` 号码池，以及 LubanSMS、SMSBower、SMSPool、FireFox；支持余额/配置检查、国家与服务选择、验证码轮询、订单完成或释放 |
| 反代配置 | 对接 sub2api，读取远端分组和代理；配置并发、优先级、负载因子、模型白名单和备注；支持标准 OAuth 批量导入及 Agent Identity 专用链路 |
| 代理配置 | 代理批量导入、自动检测、国家识别、启停与流量统计；通过功能标签分别服务注册/登录、账户检测和支付探测；临时故障只切换代理，不永久判定失效 |
| 账户管理 | 管理 Session、LS、SK、AT、RT 和换绑邮箱；批量测活、AT 检测/续期、订阅检测、试用资格、Checkout 类型与支付方式探测；筛选、排序、导出、换绑和反代操作 |
| 提链管理 | Plus、Pro、Team 与 Codex 计划的批量 Checkout 任务；支持 Hosted、PayPal、iDEAL、TWINT、UPI、PIX、MoMo、GCash、GoPay、BLIK、Kakao Pay 等路径；代理池、优惠开关、任务恢复和结果导出 |
| 支付管理 | GoPay 账号与支付生命周期、MoMo OAICS 协议提链、PayPal Billing Agreement 和直卡协议任务；展示逐账户进度、结果及实时日志 |
| 日志管理 | 以邮箱账户为核心的结构化审计日志；按类别、行为、操作者、对象和结果筛选，查看任务、请求及操作详情 |

其他界面能力包括：中英文切换、Light/Dark 主题、可拖拽表格列宽、菜单页面缓存、表单草稿保留，以及通过轮询/SSE 持续更新运行中的任务。

## 系统架构

```text
Browser
  |
  v
Go API + React static frontend (port 8000)
  |                         |
  |                         +--> PostgreSQL
  |
  +--> Python FastAPI Worker (internal port 8765)
          |
          +--> Protocol clients / Camoufox / Playwright
          +--> Mail, SMS, proxy, sub2api and payment providers
```

- **前端**：React 19、TypeScript、Vite 8、Tailwind CSS 4、GSAP、Lucide
- **后端**：Go 1.23、GORM、PostgreSQL
- **自动化 Worker**：Python 3.12+、FastAPI、curl_cffi、Playwright、Camoufox
- **部署运行**：Docker Compose；可选 Xvfb 与 noVNC 用于可视浏览器排查

Go 后端与 Python Worker 共用 PostgreSQL。账号凭据和任务结果不会依赖浏览器页面存活，刷新或切换菜单后可继续读取后端任务状态。

## 快速开始

### Docker Compose（推荐）

要求：Docker Desktop，或 Docker Engine + Docker Compose v2。

```bash
git clone https://github.com/pxygit/SunnyRegister.git
cd SunnyRegister
```

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\docker-up.ps1
```

Linux：

```bash
bash scripts/docker-up.sh
```

启动脚本会创建 `.env`、生成必要的本地密码、构建镜像并等待服务通过健康检查。完成后访问：

```text
http://127.0.0.1:8000
```

默认管理员用户名为 `admin`。本地 Docker 部署的密码保存在 `.env` 的 `ADMIN_PASSWORD` 中，启动脚本不会在终端打印密码。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f sunnyregister python-worker
```

停止服务：

```powershell
.\scripts\docker-down.ps1
```

```bash
bash scripts/docker-down.sh
```

> Docker named volume 保存 PostgreSQL 和运行数据。不要使用 `docker compose down -v`，除非确定要删除全部持久化数据并已完成备份。

### 原生运行

原生部署适合不能使用 Docker，或 Windows 上需要直接观察可视浏览器的环境。需要 Node.js 22+、Go 1.23+、Python 3.12+ 和 PostgreSQL 14+（推荐 16），并在 `.env` 中配置 `DATABASE_URL`。

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1
```

Linux：

```bash
bash scripts/start-linux.sh
```

完整要求、停止和更新方式见 [原生部署文档](./docs/NATIVE_DEPLOY.md)。

## 建议使用流程

1. 在“邮箱配置”中选择渠道、创建分组并导入邮箱凭证，先使用邮件查询确认收件正常。
2. 在“代理配置”中导入代理并完成检测，为需要的业务分配功能标签和国家信息。
3. 如任务需要手机号验证，在“接码配置”中启用自建号码池或外部供应商，并先执行配置/余额检查。
4. 如需自动导入反代，在“反代配置”中填写 sub2api 地址和 Admin Token，并加载远端分组及代理选项。
5. 在“工作台”选择邮箱、执行方式、并发数和目标阶段后创建任务，通过分账户进度与日志观察执行状态。
6. 在“账户管理”中检查 Session/Token、健康状态、订阅、试用资格、Checkout 类型和支付方式，再按需换绑、导出、反代或提链。

外部邮箱、短信、代理和支付平台都有自身限流、余额、风控与可用性约束。批量任务应先用少量测试数据验证配置，再逐步提高并发。

## 邮箱凭证格式

导入界面会按所选邮箱类型解析凭证，每行一条，字段使用 `----` 分隔。

| 类型 | 规范格式 |
| --- | --- |
| Microsoft OAuth | `email----password----client_id----refresh_token` |
| Apple iCloud / xbovo | `email----key` |
| Apple iCloud / url_api | `email`、`email----password`、`email----收码URL`，并可继续附加备用收码 URL 和 Base32 TOTP 密钥 |
| Remail | `email----serviceToken` 或受支持的 Remail 凭证 JSON |
| 自建域名邮箱 | `email----独立取件URL` |
| 自建手机号池 | `+E.164号码----收码URL` |

Microsoft 导入会识别 `client_id` 与 `refresh_token` 的常见错位并规范化。`url_api` 的第二段以 `http://` 或 `https://` 开头时按收码接口处理，否则按 ChatGPT 密码处理；具体组合和字段状态以导入弹窗提示为准。

不要在 Issue、日志截图或公开聊天中提交真实密码、Token、TOTP 密钥或收码 URL。

## 常用配置

复制 [`.env.example`](./.env.example) 为 `.env` 后按部署方式调整。高频配置如下：

| 变量 | 用途 | 默认值/说明 |
| --- | --- | --- |
| `SUNNYREGISTER_BIND` / `SUNNYREGISTER_PORT` | Web 监听地址和端口 | 本地 Compose 为 `0.0.0.0:8000` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 管理入口凭据 | 密码留空时由启动流程生成 |
| `DATABASE_URL` | PostgreSQL 连接串 | 原生部署必填 |
| `PYTHON_WORKER_URL` / `PYTHON_WORKER_TOKEN` | Go 与 Worker 的内部通信 | Compose 自动配置 Worker 地址 |
| `SUNNY_TIMEZONE` | 任务计划与显示时区 | `Asia/Shanghai` |
| `SUNNY_HEALTHCHECK_*` | 定时测活时间、并发和批次 | 详见配置模板 |
| `ENABLE_XVFB` / `ENABLE_NOVNC` | 可视浏览器与远程排查 | 本地默认关闭 |
| `OUTLOOK_IMAP_PROXY` | Outlook IMAP 专用代理 | 可选 |
| `SUB2API_*` | sub2api 默认连接配置 | 也可在 Web 控制台填写 |

生产环境请使用 [`docker-compose.production.yml`](./docker-compose.production.yml) 和 Docker secrets，不要直接公开 Go、Worker、PostgreSQL 或 noVNC 端口。

## 开发与验证

Windows 可执行与 CI 接近的完整检查和运行栈验证：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1
```

分模块执行：

```powershell
cd frontend
npm ci
npm run lint
npm run build

cd ..\backend
go test -count=1 ./...
go vet ./...

cd ..
python -m pip install --requirement python-worker\requirements-test.txt
python -m compileall -q python-worker
$env:PYTHONPATH = "python-worker"
python -m pytest -q python-worker\tests
```

CI 工作流定义在 [`.github/workflows/ci.yml`](./.github/workflows/ci.yml)，覆盖前端 lint/build、Go test/vet、Python compile/pytest 和 Docker Compose 配置校验。

## 项目结构

```text
SunnyRegister/
├── backend/          # Go API、数据模型、任务调度与第三方集成
├── frontend/         # React 管理控制台
├── python-worker/    # 协议请求、浏览器自动化、邮箱/接码/支付运行时
├── docs/             # 部署、生产运维和数据库迁移文档
├── scripts/          # 安装、启动、停止、测试与发布脚本
├── docker-compose.yml
└── docker-compose.production.yml
```

## 部署与运维文档

- [Docker 部署](./docs/DOCKER_DEPLOY.md)
- [Windows / Linux 原生部署](./docs/NATIVE_DEPLOY.md)
- [Linux 生产部署](./docs/PRODUCTION_DEPLOY.md)
- [PostgreSQL 部署与 SQLite 旧数据迁移](./docs/POSTGRESQL_MIGRATION.md)
- [安全问题报告](./SECURITY.md)
- [界面与交互设计说明](./DESIGN.md)

## 安全与数据

- `.env`、`secrets/`、`data/`、数据库、日志、导出、截图和备份不得提交到 Git。
- 邮箱密码、OAuth Token、Session、AT、RT、SK、TOTP、代理凭据和供应商密钥均属于敏感数据；应使用加密磁盘和加密异地备份，并限制服务器访问权限。
- 公网部署必须启用 HTTPS、强管理员密码和访问控制。`8765`、`5432`、`5900`、`6080` 等内部或排障端口不得直接暴露公网。
- noVNC 仅用于临时排查。远程访问应通过 SSH 隧道，使用后立即关闭。
- 修改配置或升级前先备份 PostgreSQL；Docker volume 不是备份。
- 第三方接口可能随时变更。升级后应先用模拟数据或少量授权资源验证邮箱、接码、代理、反代和支付链路。

## 免责声明

本项目不隶属于或受 OpenAI、Microsoft、LubanSMS、SMSBower、SMSPool、FireFox、sub2api 及其他第三方服务认可。使用者必须确保对所使用的账号、邮箱、手机号、代理和外部服务拥有合法授权，并遵守所在地法律及相关平台的服务条款。

项目按现状提供，不保证持续可用、任务成功率或第三方接口永久兼容。因部署、使用或修改本项目产生的账号限制、数据丢失、服务费用、合规风险及其他后果由使用者自行承担。

## License

本项目基于 [MIT License](./LICENSE) 开源。
