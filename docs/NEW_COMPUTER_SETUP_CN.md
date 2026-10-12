# 新电脑安装与旧数据迁移指南

从 GitHub 下载的是项目源码，不是安装包。新电脑需要建立 Python 环境、安装依赖、下载本地检索模型并构建前端；完成一次后，日常双击根目录的 `启动项目.bat` 即可。

本文面向 **Windows 10/11、Intel/AMD 64 位电脑**。Windows ARM、macOS、Linux 和 Docker 不属于本指南已验证的安装路线。安装阶段需要网络；安装完成后，OCR、校对、统计、导出和本地检索可在本机运行。

## 1. 先选你需要的使用方式

| 目的 | 需要做什么 |
| --- | --- |
| 在新电脑从空库开始使用 | 完成第 2～6 节，不必填写 API Key |
| 继续使用旧电脑的文档 | 完成安装后，再按第 8 节备份恢复 |
| 让模型归纳生成回答 | 在第 7 节配置云端模型；不配置时仍可检索原文 |
| 手机或其他电脑从外网访问 | 完成本机检查后，按第 9 节配置 Tailscale 和登录保护 |

**GitHub 不会带过旧电脑的正式数据库、原件、备份、API Key、登录口令、虚拟环境或本地模型。** 新安装列表为空是正常现象。

## 2. 安装基础软件

| 软件 | 本项目的选择 | 官方入口 |
| --- | --- | --- |
| Python | 3.12 或 3.13，64 位；以下命令使用 3.12 | [Windows 下载页](https://www.python.org/downloads/windows/) |
| Node.js / npm | 可使用 22.x，与本项目已有测试环境一致；npm 随 Node.js 安装 | [Node.js 下载页](https://nodejs.org/en/download) |
| Git | 使用 `git clone`、`git pull` 时需要；下载 ZIP 可不装 | [Git for Windows](https://git-scm.com/install/windows) |

按项目已验证的 Python 版本安装，不要直接用官网默认的最新大版本替代。Python 安装时保留 pip、Python Launcher，以及 Tcl/Tk（备份恢复文件选择窗口需要），并勾选添加到 PATH。Node.js、Git 安装后也应能从终端调用。

关闭旧终端，重新打开 PowerShell，检查：

```powershell
py -3.12 --version
node --version
npm.cmd --version
git --version
```

如果选择 Python 3.13，将本文唯一的 `py -3.12 -m venv .venv` 换成 `py -3.13 -m venv .venv`；后续 `.venv` 命令相同。若没有 `py`，先确认 `python --version` 为 3.12 或 3.13，再用 `python -m venv .venv`。

不需要额外安装 MySQL、Redis、CUDA、GPU 驱动或 Tesseract。数据库使用 SQLite，默认 OCR 使用 RapidOCR。

## 3. 下载源码，找到正确目录

### 方法 A：Git 克隆（方便后续更新）

在 PowerShell 中执行，示例将项目放在 `D:\Projects`；没有 D 盘时换成自己的可写文件夹：

```powershell
New-Item -ItemType Directory -Force D:\Projects
Set-Location D:\Projects
git clone https://github.com/666whatcanisay/lastStudentday.git doc-ocr-system-github
Set-Location .\doc-ocr-system-github
```

### 方法 B：下载 ZIP

打开 [GitHub 仓库](https://github.com/666whatcanisay/lastStudentday)，点击 **Code → Download ZIP**，完整解压到可写文件夹。不要直接在 ZIP 内双击启动，也不要放在需要管理员权限写入的 `Program Files` 中。

无论哪种方法，后续命令都在**项目根目录**执行。根目录应包含 `app/`、`frontend/`、`scripts/`、`requirements.txt` 和 `启动项目.bat`。ZIP 解压后的文件夹名称可以不同，启动按钮不依赖固定目录名。

## 4. 安装后端与模型

逐条执行，每一步确认成功后再继续：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe scripts\install_dependencies.py
.\.venv\Scripts\python.exe scripts\download_model.py
```

- 第一条创建这台电脑自己的 Python 虚拟环境，不需要激活它，也不要复制旧电脑的 `.venv`。
- 第二条安装依赖，最后应显示“运行时依赖自检通过”。使用该脚本而不是直接 `pip install -r requirements.txt`：它分别安装 RapidOCR 与其他依赖，避免两种 OpenCV 包互相覆盖。
- 第三条下载 BGE 中文向量模型到 `models/bge-small-zh-v1.5`，用于语义检索。**这不是 OCR 模型**；OCR 默认模型由 RapidOCR 安装包提供。

向量模型缺失时，系统会降级为 BM25 关键词检索，不代表完整的“关键词＋向量”功能已经就绪。下载中断可重新执行脚本，通过校验的已有文件会跳过。

默认下载源为 `hf-mirror.com`。若该源在新电脑不可达，而官方源可达，可在当前 PowerShell 中明确设置后重试：

```powershell
$env:HF_ENDPOINT = "https://huggingface.co"
.\.venv\Scripts\python.exe scripts\download_model.py
```

下载脚本读取进程环境变量，不自动加载 `.env`；修改 `.env` 的 `HF_ENDPOINT` 不能代替上面的终端设置。

## 5. 安装并构建前端

仍从项目根目录执行：

```powershell
Push-Location frontend
npm.cmd ci
npm.cmd run build
Pop-Location
```

`npm.cmd ci` 按锁文件安装前端依赖；构建成功后应生成 `app/static_vite/index.html`。`node_modules/` 和前端构建产物均不随 GitHub 下载，需要在新电脑生成。

本文使用 `npm.cmd`，避免 PowerShell 将 `npm` 解析成 `npm.ps1` 时遇到脚本执行策略拦截，无需为此修改全机执行策略。

## 6. 首次启动与检查

在项目根目录执行或直接双击：

```powershell
.\启动项目.bat
```

看到“服务已就绪”后，打开 <http://127.0.0.1:8000>。首次启动自动创建 `data/`、`uploads/` 和 SQLite 数据库，并执行数据库迁移，不需要手动建表。

没有配置登录保护的全新安装，默认进入本机管理员工作台，无需输入旧电脑口令。先完成以下检查：

1. 文档页的引擎显示 **RapidOCR**，不是 Mock。
2. 用 `test_corpus/` 的合成 PDF，或自行制作的非敏感图片，上传并完成真实识别。
3. 查看原件，修改一个字段并保存，确认版本变化、历史仍保留。
4. 查看统计，下载一份 PDF 和 Excel 并打开检查。
5. 提问后能检索到刚上传的内容；未配置云端 Key 时显示“仅检索”是正常行为。核对问答页/日志确认向量模型没有加载失败、没有降级为 BM25 单路。

停止前等待识别、导出和问答任务结束，再双击 `scripts/stop_all.bat`。今后无需重复安装，双击根目录启动按钮即可；它不会设置 Windows 登录自启动。

## 7. 可选配置：云端回答与数据目录

**仅用本地功能，可以不创建 `.env`。** 需要配置时，确保在当前项目根目录创建，已有文件不要覆盖：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item .env.example .env
}
notepad .env
```

### 云端生成式问答

参考 [.env.example](../.env.example)，填写与你的模型服务一致的三项：

```dotenv
LLM_BASE_URL=模型服务的OpenAI兼容接口地址
LLM_API_KEY=你自己的API密钥
LLM_MODEL=模型名称
```

这些是占位说明，必须替换为实际值。保存后停止并重新启动服务。云端 Key 不是 OCR 的要求；不填仍可识别、校对、统计、导出和检索。

主动发送云端问答会将相关文档片段和上下文发送给配置的服务。真实 Key 只放在本机 `.env`，不要写入 `.env.example`、截图或提交到 GitHub。若问答页面仍提示 `doc-ocr-system\.env`，以**当前项目根目录的 `.env`**为准，这是旧目录名提示。

### 独立数据目录

默认数据库和原件放在项目根目录。需要放到其他磁盘时，可在 `.env` 中设置：

```dotenv
DOC_OCR_DATA_DIR=D:/DocOCRData
```

填写同时容纳 `data/` 和 `uploads/` 的数据根，不要填写 `.db` 文件。终端中已设置的同名环境变量优先于 `.env`。更改目录不会自动搬迁旧数据，已有资料请按第 8 节备份恢复；模型仍位于项目的 `models/` 下。

## 8. 从旧电脑迁移文档

1. 在旧电脑启动当前版，进入“数据管理”，点击 **立即备份并下载**。备份 ZIP 包含数据库、原件、回收站、版本历史和问答记录；另存一份安全副本。
2. 将 ZIP 安全复制到新电脑。先按第 2～6 节完成安装并确认空项目能运行。
3. 若要用独立数据目录，先在新电脑设置 `DOC_OCR_DATA_DIR`，之后启动、停止和恢复使用同一配置。
4. 等待新电脑任务结束，运行 `scripts/stop_all.bat`。双击 `scripts/restore_backup.bat`，选择备份 ZIP，确认整体替换目标数据。
5. 脚本校验路径、清单哈希和数据库，保留恢复前快照，调整原件路径并执行迁移；失败时尝试回滚。只使用本项目生成且版本受支持的备份，不要自行改 ZIP 内容。
6. 恢复成功后双击 `启动项目.bat`，核对文档数量，打开一份原件、查看历史、尝试检索。索引未就绪时等待后台校验/建立，或使用页面索引入口。

不要只复制一个 `.db` 文件：它不能代替原件，旧的绝对路径也可能失效。恢复会替换目标数据，不合并新旧两套文档；先备份新电脑上需要保留的资料。

备份**不包含** API Key、登录配置、模型、前端依赖和虚拟环境，这些需要重新安装或单独安全配置。不要直接照搬旧 `.env` 中的绝对数据路径、代理或旧设备域名。

备份未加密，含全部文档内容；请使用受控介质或安全传输方式，不上传到公开仓库。

## 9. 可选：远程访问和登录保护

本机使用不需要 Tailscale。需要手机或其他电脑通过公网访问时，按 [Tailscale Funnel 操作卡](TAILSCALE_DEMO.md)完整执行：安装并登录客户端、确定**新设备**域名、配置登录口令、本机验证未登录受保护接口返回 401，再开启 Funnel。

新电脑不会自动继承旧设备的 Funnel 域名与服务映射。不要照抄旧电脑的域名，不要把应用改为监听 `0.0.0.0`，也不要未配置口令就对外开放。

当前单口令模式登录即完整管理员，没有独立账号或操作人区分。远程配置是可选的；详尽命令集中维护在操作卡中。

## 10. 常见问题与日常更新

| 现象 | 怎么检查/处理 |
| --- | --- |
| 找不到 `py`、`python`、`node` 或 `git` | 安装对应软件并重新打开终端；确认命令版本，ZIP 下载方式不要求 Git |
| `.venv` 或依赖安装失败 | 确认 Python 为 3.12/3.13、64 位，检查网络；使用 `install_dependencies.py`，不要混用全局 pip |
| 双击启动提示前端未构建 | 在 `frontend/` 完成 `npm.cmd ci`、`npm.cmd run build`，确认 `app/static_vite/index.html` 存在 |
| 引擎是 Mock 或 OCR 不可用 | 查看依赖安装自检及启动日志，修复 RapidOCR 后重启；不要用模拟结果当正式识别 |
| 问答只有检索片段，没有生成回答 | 未配置云端 Key 时的正常降级；检查 `.env` 三项配置并重启 |
| 日志提示向量模型加载失败 | 重跑下载脚本，检查模型目录；接口返回 200 不等于向量检索已就绪 |
| 首次启动列表为空 | 全新安装没有旧数据；需要旧文档时按第 8 节恢复 |
| 8000 端口被占用 | 已核验为本项目的进程会复用；未知服务不自动结束。确认归属或使用下方其他端口命令 |
| 恢复没有弹出选择窗口 | 检查 Python 是否安装 Tcl/Tk；也可用下方命令行方式指定备份 |
| 中文乱码或启动失败 | 查看数据根下 `data/runtime/server.stderr.log`；使用仓库原始启动脚本，不随意改编码 |

其他端口启动示例（启动前先确认没有同数据目录服务在运行）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_all.ps1 -Port 8001
```

启动后访问 <http://127.0.0.1:8001>。同一数据根只能有一个服务；换端口不能绕过数据目录锁。

无文件选择窗口时的恢复方式（先停服，换成你自己的 ZIP 路径）：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\restore_backup.py "D:\Backups\你的备份.zip"
```

Git 安装方式后续更新：先备份、等待任务完成并停服，确认自己的修改已保存，再在项目根目录执行：

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe scripts\install_dependencies.py
.\.venv\Scripts\python.exe scripts\download_model.py
Push-Location frontend
npm.cmd ci
npm.cmd run build
Pop-Location
.\启动项目.bat
```

如果 Git 提示本地修改或分支分歧，不要执行强制重置，先保留自己的修改。ZIP 安装方式更新时，新解压目录不会自动带过私有配置和数据，按第 7～8 节处理。

部署自检通过不等于所有票据字段都准确。金额、购销方、币种等关键字段仍需对照原件；详细技术边界见 [技术与工作流](TECHNICAL_WALKTHROUGH.md)。日常维护见 [本机交付说明](LOCAL_DELIVERY.md)，准备讲解见 [项目学习指南](PROJECT_LEARNING_GUIDE_CN.md)。
