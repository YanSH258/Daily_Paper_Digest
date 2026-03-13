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
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "daily-paper-digest=main:main",
            "stat-db=utils.stat_db:main",
        ],
    },
)
