"""共享可写路径：源码默认仓库根，安装版默认用户数据根。

DPD_DATA_ROOT 可覆盖两种运行方式。配置中的相对路径均相对于该根目录，
与当前工作目录或配置文件所在目录无关；包内资源只用于读取模板。
"""
import os
import sys
from importlib import resources
from pathlib import Path


def _source_root():
    module = Path(__file__).resolve()
    root = module.parents[2]
    if (module.parent.parent.name == "src"
            and (root / "setup.py").is_file()
            and (root / "src" / "main.py").is_file()
            and (root / "config" / "config_template.yaml").is_file()):
        return root
    return None


def _user_data_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        # XDG 规范要求绝对路径；忽略无效的相对值。
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    return base / "daily-paper-digest"


SOURCE_ROOT = _source_root()
_override = os.environ.get("DPD_DATA_ROOT")
DATA_ROOT = (Path(_override).expanduser().resolve() if _override
             else SOURCE_ROOT or _user_data_root())
# 保留旧调用方的名称；现在表示有效数据根，而非安装目录。
PROJECT_ROOT = DATA_ROOT


def resolve_against_root(p) -> Path:
    """展开 ~；绝对路径保留，相对路径锚定到有效数据根。"""
    path = Path(p).expanduser()
    return path if path.is_absolute() else DATA_ROOT / path


def resolve_config_paths(config: dict) -> dict:
    """将运行时配置中的路径统一为绝对路径，不增加缺失的配置项。"""
    for section, key in (("database", "path"), ("output", "output_dir"),
                         ("fetcher", "upload_dir"), ("backup", "directory")):
        value = config.get(section, {}).get(key)
        if value and not (section == "database" and value == ":memory:"):
            config[section][key] = str(resolve_against_root(value))
    return config


def config_template_text() -> str:
    """读取源码或 wheel 中的只读模板，不依赖当前工作目录。"""
    if SOURCE_ROOT is not None:
        return (SOURCE_ROOT / "config" / "config_template.yaml").read_text(encoding="utf-8")
    return resources.files("dpd_resources").joinpath("config_template.yaml").read_text(encoding="utf-8")


def init_config(path="config/config.yaml") -> Path:
    """显式初始化用户配置；独占创建，已有文件绝不覆盖。"""
    destination = resolve_against_root(path)
    template = config_template_text()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(template)
    return destination
