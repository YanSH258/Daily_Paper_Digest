"""项目路径解析：配置中的相对路径统一锚定到项目根，不受进程工作目录影响。

背景：output_dir 等相对路径此前在不同模块里有两种锚定基准（进程 cwd /
src/ 目录），导致日报被写到 src/data/output 而预览却读 data/output。
所有相对路径解析一律走 resolve_against_root。
"""
from pathlib import Path

# 源码布局 <项目根>/src/utils/paths.py → parents[2] = 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_against_root(p) -> Path:
    """绝对路径原样返回；相对路径锚定到项目根。"""
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path
