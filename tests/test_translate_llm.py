"""标题翻译改走已配置 LLM 的回归测试。

全部使用 mock，不联网、不调用真实模型：
- translate_titles_llm 一次批量调用解析编号译文并映射回原标题；
- 中文标题与空标题不进入翻译请求；
- LLM 未配置（缺 api_key/base_url）时不构造客户端、返回空映射；
- LLM 调用失败（重试一次后仍失败）返回空映射，调用方可回退免费接口；
- 批量翻译接口（_batch_translate_titles）provider=auto 时优先 LLM 并
  写入 title_zh；LLM 未译出的条目回退 MyMemory 逐条补译。
"""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils import translate as translate_mod  # noqa: E402
import web_server  # noqa: E402

LLM_CFG = {
    "llm": {
        "provider": "deepseek",
        "deepseek": {
            "api_key": "sk-test",
            "base_url": "https://api.example.com",
            "model": "test-model",
        },
    }
}


def _make_config(tmp: str) -> str:
    cfg = Path(tmp) / "config.yaml"
    cfg.write_text(
        f"""
database:
  path: {Path(tmp) / 't.db'}
llm:
  provider: deepseek
  deepseek:
    api_key: sk-test
    base_url: https://api.example.com
    model: test-model
relevance_threshold: 4
output:
  output_dir: {Path(tmp) / 'output'}
""",
        encoding="utf-8",
    )
    return str(cfg)


def _fake_llm_client(content: str = None, exc: Exception = None):
    """假 OpenAI 客户端：返回固定译文文本，或每次调用都抛异常。

    content 为 None 时按请求里的编号行数自动生成 "i. 译文i"。
    """

    def create(**kwargs):
        if exc is not None:
            raise exc
        text = content
        if text is None:
            user_msg = kwargs["messages"][-1]["content"]
            n = sum(1 for line in user_msg.splitlines() if line.strip())
            text = "\n".join(f"{i}. 译文{i}" for i in range(1, n + 1))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
        )

    create_call = unittest.mock.Mock(side_effect=create)
    return (
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_call))),
        create_call,
    )


class TranslateTitlesLlmTests(unittest.TestCase):
    def test_parses_numbered_output_and_maps_back(self):
        client, create_call = _fake_llm_client()
        with unittest.mock.patch.object(translate_mod, "_llm_client", return_value=client):
            result = translate_mod.translate_titles_llm(
                ["Catalytic methane activation", "Perovskite lattice dynamics"], LLM_CFG
            )
        self.assertEqual(
            result,
            {
                "Catalytic methane activation": "译文1",
                "Perovskite lattice dynamics": "译文2",
            },
        )
        kwargs = create_call.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertIn("1. Catalytic methane activation", kwargs["messages"][-1]["content"])

    def test_skips_chinese_and_empty_titles(self):
        client, create_call = _fake_llm_client()
        with unittest.mock.patch.object(translate_mod, "_llm_client", return_value=client):
            result = translate_mod.translate_titles_llm(
                ["量子点研究进展", "", "Catalytic methane activation"], LLM_CFG
            )
        self.assertEqual(result, {"Catalytic methane activation": "译文1"})
        sent = create_call.call_args.kwargs["messages"][-1]["content"]
        self.assertNotIn("量子点", sent)

    def test_llm_not_configured_returns_empty_without_client(self):
        with unittest.mock.patch.object(
            translate_mod, "_llm_client"
        ) as client_factory:
            result = translate_mod.translate_titles_llm(
                ["Some English Title"], {"llm": {"provider": "deepseek", "deepseek": {}}}
            )
        self.assertEqual(result, {})
        client_factory.assert_not_called()

    def test_llm_failure_retries_then_returns_empty(self):
        client, create_call = _fake_llm_client(exc=RuntimeError("connection error"))
        with unittest.mock.patch.object(translate_mod, "_llm_client", return_value=client):
            result = translate_mod.translate_titles_llm(["Some English Title"], LLM_CFG)
        self.assertEqual(result, {})
        self.assertEqual(create_call.call_count, 2)  # 失败后重试一次

    def test_missing_lines_are_left_for_fallback(self):
        client, _call = _fake_llm_client(content="1. 只有一句译文")
        with unittest.mock.patch.object(translate_mod, "_llm_client", return_value=client):
            result = translate_mod.translate_titles_llm(["Title One", "Title Two"], LLM_CFG)
        self.assertEqual(result, {"Title One": "只有一句译文"})


class BatchTranslateEndpointTests(unittest.TestCase):
    """回归：文献库「译标题」按钮的完整路径（自动选择 LLM 优先）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = web_server.WebContext(config_path=_make_config(self._tmp.name))
        self.ids = self.ctx.db.save_articles_batch([
            {
                "doi": f"10.1234/t{i}",
                "title": title,
                "journal": "J. Test",
                "pub_date": "2026-09-28",
                "url": f"https://example.org/{i}",
            }
            for i, title in enumerate(
                [
                    "Catalytic methane activation",
                    "Perovskite lattice dynamics",
                    "量子点研究进展",
                ],
                start=1,
            )
        ])

    def test_auto_provider_prefers_llm_and_writes_title_zh(self):
        client, _call = _fake_llm_client()
        with unittest.mock.patch.object(translate_mod, "_llm_client", return_value=client), \
                unittest.mock.patch.object(translate_mod, "translate_mymemory") as mm, \
                unittest.mock.patch("time.sleep"):
            payload, code = web_server._batch_translate_titles(
                self.ctx, {"ids": self.ids, "limit": 10}
            )
        self.assertEqual(code, 200)
        self.assertEqual(payload["provider"], "llm")
        self.assertEqual(payload["translated"], 2)
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["failed"], 0)
        mm.assert_not_called()  # LLM 全部译出，不应再碰免费接口
        rows = self.ctx.db.get_articles_by_ids(self.ids)
        zh = {r["id"]: r.get("title_zh") for r in rows}
        self.assertEqual(zh[self.ids[0]], "译文1")
        self.assertEqual(zh[self.ids[1]], "译文2")
        # 中文标题直接回填原文
        self.assertEqual(zh[self.ids[2]], "量子点研究进展")

    def test_llm_failure_falls_back_to_mymemory(self):
        client, _call = _fake_llm_client(exc=RuntimeError("llm down"))
        with unittest.mock.patch.object(translate_mod, "_llm_client", return_value=client), \
                unittest.mock.patch.object(
                    translate_mod, "translate_mymemory", return_value="机译标题"
                ), \
                unittest.mock.patch("time.sleep"):
            payload, code = web_server._batch_translate_titles(
                self.ctx, {"ids": self.ids, "limit": 10}
            )
        self.assertEqual(code, 200)
        self.assertEqual(payload["provider"], "llm")
        self.assertEqual(payload["translated"], 2)
        self.assertEqual(payload["failed"], 0)
        rows = self.ctx.db.get_articles_by_ids(self.ids)
        zh = {r["id"]: r.get("title_zh") for r in rows}
        self.assertEqual(zh[self.ids[0]], "机译标题")
        self.assertEqual(zh[self.ids[1]], "机译标题")

    def test_no_ids_translates_untranslated_candidates(self):
        """不传 ids 时按未译条目列表翻译（旧入口行为保持）。"""
        client, _call = _fake_llm_client()
        with unittest.mock.patch.object(translate_mod, "_llm_client", return_value=client), \
                unittest.mock.patch("time.sleep"):
            payload, code = web_server._batch_translate_titles(self.ctx, {"limit": 5})
        self.assertEqual(code, 200)
        self.assertEqual(payload["provider"], "llm")
        self.assertEqual(payload["translated"], 2)


if __name__ == "__main__":
    unittest.main()
