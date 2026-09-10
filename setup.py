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

with open("requirement.txt", encoding="utf-8") as f:
    requirements = [
        line.strip()
        for line in f
        if line.strip() and not line.startswith("#")
    ]

setup(
    name="daily-paper-digest",
    version="0.1.0",
    description="AI 驱动的化学/材料文献日报工具",
    packages=sorted(set(find_packages(where="src")) | {"webassets"}),
    package_dir={"": "src", "webassets": "src/static"},
    package_data={"webassets": ["*.html", "*.ico", "css/*", "js/*"]},
    py_modules=["main", "web_server", "mcp_server"],
    python_requires=">=3.9",
    install_requires=requirements,
    extras_require={
        # MCP 服务器（供 AI agent 调用文献工作台）；SDK 2.x 改了 API，锁定 1.x
        "mcp": ["mcp>=1.2,<2"],
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
