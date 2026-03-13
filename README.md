# 📚 Daily Paper Digest (AI 化学/材料文献日报)

专为科研人员打造的自动化文献追踪工具。自动监控顶刊 RSS，利用大模型（DeepSeek/Qwen）进行相关性打分与深度解读，并生成排版精美的日报。

## ✨ 核心特性

* **多源聚合**：内置 30+ 化学/材料/物理领域顶级期刊 RSS。
* **智能过滤 & 深度解读**：依据设定的研究方向自动打分（0-10分）剔除无关文章；自动绕过反爬抓取全文，精准提取研究动机、方法流程与核心数据。
* **HTML 优先分层回退**：全文获取按 HTML → 浏览器渲染 → PDF 文本提取 → 手动上传四级回退，并记录 `best_available_format` 与获取状态码。
* **校园网/代理支持**：通过环境变量配置代理，自动识别 `network_mode`（public / campus_proxy / vpn）。
* **手动上传兜底**：自动获取失败时，支持通过 CLI 上传本地 PDF/HTML/TXT 文件进入同一解析流水线。
* **防瞎编 & 数据大盘**：若遇版权墙仅有摘要，严格限制 AI 凭空捏造，并在解读结果中标注证据等级（FULLTEXT / ABSTRACT_ONLY）。内置 SQLite 数据库，一键统计热点趋势并支持 Excel 导出。

## 🚀 快速开始

**1. 环境安装**
确保使用 Python 3.9+，克隆项目后安装依赖：
```
pip install -r requirement.txt
```

**2. 配置秘钥**
复制 `config_template.yaml` 并重命名为 `config.yaml`，填入你的大模型 API Key，并修改 `research_topics` 为你的研究方向。

**3. 运行指令**

* 运行抓取与推送：`python main.py`
* 查看本地数据库大盘：`python stat_db.py`
* 搜索并导出为 Excel：`python stat_db.py --search "关键字" --export my_papers.csv`

## 🌐 校园网 / 代理配置

当需要通过机构代理或 VPN 访问受版权保护的文献时，设置以下环境变量即可，**无需修改代码**：

```bash
# Linux / macOS
export HTTP_PROXY="http://proxy.your-institution.edu:8080"
export HTTPS_PROXY="http://proxy.your-institution.edu:8080"
export NO_PROXY="localhost,127.0.0.1"

# Windows (PowerShell)
$env:HTTPS_PROXY = "http://proxy.your-institution.edu:8080"
```

程序启动后会自动检测代理并在日志中记录 `network_mode`（`public` / `campus_proxy` / `vpn`）和 `access_path`（`direct` / `proxy`）。

> **注意**：请确保你拥有通过该代理访问所请求资源的合法权限。

## 📄 全文获取链路说明（HTML 优先 + 分层回退）

每篇通过相关性筛选的文章，全文获取按以下优先级依次尝试：

```
1. HTML 轻量请求（requests + 代理）
       ↓ 失败（403/JS 动态/文本过短）
2. 浏览器渲染（DrissionPage Chromium）
       ↓ 失败（Cloudflare/版权墙）
3. PDF 文本提取（PyMuPDF，需 article 含 pdf_url）
       ↓ 扫描版 PDF → OCR_NEEDED（框架占位，需安装 ocrmypdf）
4. 全部失败 → WAITING_USER_UPLOAD（建议手动上传）
```

每篇文章最终会被标记 `best_available_format`：

| 值 | 含义 |
|---|---|
| `html_fulltext` | HTML 全文成功 |
| `pdf_text` | PDF 文本层提取成功 |
| `pdf_ocr` | 扫描版 PDF，已进入 OCR 流程 |
| `abstract_only` | 仅有摘要（LLM 将以摘要模式解读）|
| `manual_uploaded` | 用户手动上传 |

LLM 解读结果中会同步标注 `evidence_level`（`FULLTEXT` / `ABSTRACT_ONLY`）。

## 📤 手动上传文献文件（兜底入口）

当自动获取链路全部失败时，可通过 CLI 手动上传本地文献文件，上传后**进入与自动抓取完全一致的解析流水线**：

```bash
# 上传 PDF
python -m fetchers.manual_upload --file /path/to/paper.pdf --doi 10.1021/jacs.xxxxx

# 上传 HTML 文件
python -m fetchers.manual_upload --file /path/to/paper.html

# 上传纯文本
python -m fetchers.manual_upload --file /path/to/paper.txt
```

支持格式：`.pdf`、`.html`、`.htm`、`.txt`（单文件上限 50 MB）。

上传成功后，文章来源标记为 `source=manual`，`best_available_format=manual_uploaded`。

## 🔒 进阶使用与部署

* **突破知网/Elsevier拦截**：使用真实浏览器访问被墙期刊，导出 Cookie 并存为根目录的 `cookies.json`，爬虫即可免密抓取。
* **GitHub Actions 云端部署**：支持全自动定时推送。⚠️ 注意：务必在 `.gitignore` 中忽略密码文件，绝对不要将 `config.yaml` 和 `cookies.json` 传到公共仓库，应将其配置在 GitHub Secrets 中！
* **OCR 支持（可选）**：当 PDF 为扫描版时系统会返回 `OCR_NEEDED` 状态。如需自动 OCR，请额外安装 `ocrmypdf` 和 `tesseract`，并在 `fetchers/pdf_fetcher.py` 中接入 OCR 入口（已预留扩展点）。

## ⚠️ 已知限制

* **OCR**：当前仅检测扫描版并返回 `OCR_NEEDED` 状态，OCR 执行需额外安装 `ocrmypdf`/`tesseract`（未内置，以减小默认依赖）。
* **校园网代理**：代理仅对 requests 层（HTML/PDF 轻量请求）生效；DrissionPage 浏览器层的代理需在系统层或 `ChromiumOptions` 中单独配置。
* **付费文献**：技术手段无法突破无授权的付费墙；建议配合机构账号 Cookie 或 EZproxy 使用。