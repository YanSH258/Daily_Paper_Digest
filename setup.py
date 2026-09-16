"""
setup.py - 项目安装配置

安装为可编辑包（开发模式）：
    pip install -e .

安装后可直接运行：
    daily-paper-digest
    daily-paper-digest --schedule
    daily-paper-digest --date 2024-01-15
"""
from setuptools import setup, find_packages

with open("requirements.txt", encoding="utf-8") as f:
    # 只取运行时段；"# ----" 之后是开发/测试依赖，不进入 wheel 依赖声明。
    runtime_section = f.read().split("# ---- 以下为开发")[0]
requirements = [
    line.strip()
    for line in runtime_section.splitlines()
    if line.strip() and not line.startswith("#")
]

setup(
    name="daily-paper-digest",
    version="0.1.0",
    description="AI 驱动的化学/材料文献日报工具",
    packages=sorted(set(find_packages(where="src")) | {"webassets", "dpd_resources"}),
    package_dir={"": "src", "webassets": "src/static", "dpd_resources": "config"},
    # 只打包公开模板，不能将本地 config.yaml 或凭据收入 wheel。
    include_package_data=False,
    package_data={
        "webassets": ["*.html", "*.ico", "css/*", "js/*"],
        "dpd_resources": ["config_template.yaml"],
    },
    py_modules=["main", "web_server", "mcp_server"],
    python_requires=">=3.9",
    install_requires=requirements,
    extras_require={
        # MCP 服务器（供 AI agent 调用文献工作台）；SDK 2.x 改了 API，锁定 1.x
        # 当前 MCP SDK 测试/运行基线为 Python 3.10+；核心包仍支持 Python 3.9。
        "mcp": ["mcp>=1.2,<2; python_version >= '3.10'"],
    },
    entry_points={
        "console_scripts": [
            "daily-paper-digest=main:main",
            "stat-db=utils.stat_db:main",
            "daily-paper-web=web_server:main",
            "daily-paper-mcp=mcp_server:main",
        ],
    },
)
