# 📚 Daily Paper Digest (AI 化学/材料文献日报)

专为科研人员打造的自动化文献追踪工具。自动监控顶刊 RSS，利用大模型（DeepSeek/Qwen）进行相关性打分与深度解读，并生成排版精美的日报。

## ✨ 核心特性

* **多源聚合**：内置 30+ 化学/材料/物理领域顶级期刊 RSS。
* **智能过滤 & 深度解读**：依据设定的研究方向自动打分（0-10分）剔除无关文章；自动绕过反爬抓取全文，精准提取研究动机、方法流程与核心数据。
* **防瞎编 & 数据大盘**：若遇版权墙仅有摘要，严格限制 AI 凭空捏造。内置 SQLite 数据库，一键统计热点趋势并支持 Excel 导出。

## 🚀 快速开始

**1. 环境安装**
确保使用 Python 3.9+，克隆项目后安装依赖：
pip install -r requirement.txt

**2. 配置秘钥**
复制 `config_template.yaml` 并重命名为 `config.yaml`，填入你的大模型 API Key，并修改 `research_topics` 为你的研究方向。

**3. 运行指令**

* 运行抓取与推送：python main.py
* 查看本地数据库大盘：python stat_db.py
* 搜索并导出为 Excel：python stat_db.py --search "关键字" --export my_papers.csv

## 🔒 进阶使用与部署

* **突破知网/Elsevier拦截**：使用真实浏览器访问被墙期刊，导出 Cookie 并存为根目录的 `cookies.json`，爬虫即可免密抓取。
* **GitHub Actions 云端部署**：支持全自动定时推送。⚠️ 注意：务必在 `.gitignore` 中忽略密码文件，绝对不要将 `config.yaml` 和 `cookies.json` 传到公共仓库，应将其配置在 GitHub Secrets 中！