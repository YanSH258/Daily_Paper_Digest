# 📚 Daily Paper Digest (AI 化学/材料文献日报)

专为科研人员打造的自动化文献追踪工具。自动监控顶刊 RSS，基于摘要利用大模型（DeepSeek/Qwen）进行相关性打分与解读，并生成日报推送。

## ✨ 核心特性

* **多源聚合**：内置 30+ 化学/材料/物理领域顶级期刊 RSS。
* **摘要过滤 & AI 解读**：基于摘要依据研究方向自动打分（0-10分），剔除无关文章，对相关文章进行 AI 深度解读。
* **数据大盘**：内置 SQLite 数据库，一键生成 HTML 大盘，支持关键词搜索与 CSV 导出。
* **多渠道推送**：支持邮件（附带 `paper_index.html` 附件）和飞书 Webhook 推送。

## 📁 项目目录结构

```
Daily_Paper_Digest/
├── src/
│   ├── main.py                    # 主入口
│   ├── core/
│   │   ├── fetcher.py             # RSS 抓取
│   │   ├── analyzer.py            # LLM 相关性过滤与解读
│   │   ├── db.py                  # SQLite 数据库操作
│   │   ├── notifier.py            # 报告生成与推送
│   │   ├── rate_limiter.py        # 速率限制
│   │   ├── request_manager.py     # HTTP 请求管理
│   │   └── ua_pool.py             # User-Agent 池
│   ├── fetchers/
│   │   ├── oa_fetcher.py          # OpenAlex 摘要补全
│   │   ├── network.py             # 代理检测
│   │   └── models.py              # 数据模型与状态码
│   └── utils/
│       ├── stat_db.py             # 数据库统计 & HTML 大盘生成
│       └── reanalyze.py           # 重新触发 AI 解读
│
├── data/                          # 数据目录（gitignore）
│   ├── db/                        # SQLite 数据库
│   ├── logs/                      # 运行日志
│   └── output/                    # 生成的报告文件
│
├── config/
│   ├── config_template.yaml       # 配置模板
│   └── config.yaml                # 实际配置（gitignore，勿提交）
│
├── requirement.txt
└── setup.py
```

## 🚀 快速开始

**1. 安装依赖**

```bash
pip install -r requirement.txt
```

**2. 配置**

```bash
cp config/config_template.yaml config/config.yaml
# 填入 API Key，修改 research_topics 为你的研究方向
```

**3. 运行**

```bash
# 立即运行一次
python src/main.py

# 每日定时运行（按 config.yaml 中的时间）
python src/main.py --schedule

# 其他选项
python src/main.py --date 2025-01-15        # 指定报告日期
python src/main.py --config path/to/cfg.yaml

# 查看数据库大盘（生成 data/output/paper_index.html）
python src/utils/stat_db.py

# 搜索并导出 CSV
python src/utils/stat_db.py --search "关键字" --export my_papers.csv
```

**服务器定时运行（cron）**

```bash
# 编辑 crontab
crontab -e

# 每天早上 8:00 运行，日志写入 data/logs/cron.log
0 8 * * * cd /path/to/Daily_Paper_Digest && /path/to/python src/main.py >> data/logs/cron.log 2>&1
```

> 用 `which python` 获取 Python 路径；如果使用虚拟环境，替换为 `/path/to/venv/bin/python`。

## 📬 邮件推送

邮件正文为当日 HTML 日报，同时附带 `paper_index.html` 数据库大盘文件。在 `config.yaml` 中启用：

```yaml
output:
  email:
    enabled: true
    smtp_server: "smtp.163.com"
    smtp_port: 465
    username: "YOUR_EMAIL@163.com"
    password: "YOUR_AUTH_CODE"
    recipients:
      - "TARGET_EMAIL@qq.com"
```

## ⚠️ 注意事项

* **GitHub Actions 部署**：不要将 `config/config.yaml` 提交到公开仓库，应将 API Key 和邮件密码配置在 GitHub Secrets 中。
* **代理**：如需通过机构代理访问，设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量即可。
