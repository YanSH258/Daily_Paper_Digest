# -*- coding: utf-8 -*-
"""topic_groups 自定义子方向分组：configure_topic_groups / classify_article /
active_topic_names / Notifier 构造钩子。

topic_groups 只影响 topic 标签与展示分组，不参与相关性评分；配置非法必须
回退内置表并告警，模块级表是全局状态，每个用例结束都要恢复内置表。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import notifier  # noqa: E402
from core.notifier import (  # noqa: E402
    Notifier,
    active_topic_names,
    classify_article,
    configure_topic_groups,
)

CUSTOM_GROUPS = [
    {"name": "催化 / 反应机理", "keywords": ["photocatalysis", "electrocatalyst"]},
    {"name": "电池材料", "keywords": ["solid-state electrolyte"]},
]


class TestTopicGroups(unittest.TestCase):
    def setUp(self):
        # 模块级分类表是全局状态：每个用例结束恢复内置表
        self.addCleanup(configure_topic_groups, {})

    def test_builtin_default(self):
        configure_topic_groups({})
        self.assertIn("机器学习势函数 / MLIP", active_topic_names())
        self.assertEqual(active_topic_names()[-1], "其他")
        a = {"title": "A universal machine learning potential for metals", "abstract": ""}
        self.assertEqual(classify_article(a), "机器学习势函数 / MLIP")

    def test_custom_groups_override(self):
        configure_topic_groups({"topic_groups": CUSTOM_GROUPS})
        names = active_topic_names()
        self.assertEqual(names, ["催化 / 反应机理", "电池材料", "其他"])
        hit = {"title": "Photocatalysis for CO2 reduction", "abstract": ""}
        self.assertEqual(classify_article(hit), "催化 / 反应机理")
        miss = {"title": "Something entirely unrelated", "abstract": ""}
        self.assertEqual(classify_article(miss), "其他")  # 兜底不参与匹配

    def test_builtin_keyword_not_matched_after_override(self):
        configure_topic_groups({"topic_groups": CUSTOM_GROUPS})
        a = {"title": "A universal machine learning potential for metals", "abstract": ""}
        self.assertEqual(classify_article(a), "其他")

    def test_reset_to_builtin_when_removed(self):
        configure_topic_groups({"topic_groups": CUSTOM_GROUPS})
        configure_topic_groups({})  # 配置项移除 → 恢复内置
        self.assertIn("机器学习势函数 / MLIP", active_topic_names())

    def test_invalid_config_falls_back_to_builtin(self):
        builtin_names = list(active_topic_names())
        for bad in (
            "not-a-list",
            [{"keywords": ["x"]}],               # 缺 name
            [{"name": "空关键词", "keywords": []}],
            [{"name": "合法", "keywords": ["ok"]}, "junk"],  # 混入非对象
        ):
            with self.subTest(bad=bad):
                configure_topic_groups({"topic_groups": bad})
                self.assertEqual(active_topic_names(), builtin_names)

    def test_user_provided_other_deduplicated(self):
        groups = CUSTOM_GROUPS + [{"name": "其他", "keywords": ["whatever"]}]
        configure_topic_groups({"topic_groups": groups})
        names = active_topic_names()
        self.assertEqual(names.count("其他"), 1)
        self.assertEqual(names[-1], "其他")

    def test_idempotent(self):
        configure_topic_groups({"topic_groups": CUSTOM_GROUPS})
        names = list(active_topic_names())
        configure_topic_groups({"topic_groups": CUSTOM_GROUPS})
        self.assertEqual(active_topic_names(), names)

    def test_notifier_construction_applies_groups(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = {"output": {"output_dir": tmp}, "topic_groups": CUSTOM_GROUPS}
        Notifier(cfg)
        hit = {"title": "Sulfide solid-state electrolyte for batteries", "abstract": ""}
        self.assertEqual(classify_article(hit), "电池材料")

    def test_articles_api_returns_active_topic_names(self):
        """前端方向下拉的数据源：/api/articles 响应携带当前生效的子方向表。"""
        from types import SimpleNamespace

        from core.db import Database
        import web_server

        db = Database(":memory:")
        self.addCleanup(db.close)
        configure_topic_groups({"topic_groups": CUSTOM_GROUPS})
        ctx = SimpleNamespace(connect_db=lambda: db._memory_conn)
        data = web_server._list_articles(ctx, {})
        self.assertEqual(data["topic_names"],
                         ["催化 / 反应机理", "电池材料", "其他"])


if __name__ == "__main__":
    unittest.main()
