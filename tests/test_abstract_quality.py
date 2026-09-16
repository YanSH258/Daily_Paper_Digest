"""摘要清洗与翻译质量门槛回归。

覆盖实际触发条件：
- APS RSS 摘要包装噪音（作者列表前缀/粘连、文末出版信息）
- RSS 截断摘要识别（决定补全链是否触发）
- 翻译质量门槛（旧规则 len<120 或中文占比<60% 会误杀正常短译文）
- OpenAlex→Crossref 补全链的选优语义（不外网请求，mock 两个源）
"""
import unittest
from unittest.mock import patch

from core.analyzer import LLMAnalyzer
from core.fetcher import clean_feed_abstract
import fetchers.oa_fetcher as oa


class CleanFeedAbstractTests(unittest.TestCase):
    def test_author_prefix_with_glued_body(self):
        # APS 实际形态：作者列表与正文粘连（FinkeldeiWe report）
        raw = ("Author(s): J. Finkeldei, B. Cao, and K. ReuterWe report "
               "the first implementation of a machine learning potential "
               "for solid-state battery interfaces.")
        out = clean_feed_abstract(raw)
        self.assertTrue(out.startswith("We report"), out)
        self.assertNotIn("Author", out)
        self.assertNotIn("Finkeldei", out)

    def test_author_prefix_glued_without_verb_fallback(self):
        # 动词表未命中时，兜底在小写-大写粘连处切开
        raw = "Author(s): A. Smith and B. JonesNickel oxide surfaces were studied."
        out = clean_feed_abstract(raw)
        self.assertTrue(out.startswith("Nickel"), out)

    def test_plain_abstract_untouched(self):
        raw = ("Electrochemical impedance spectroscopy reveals the SEI "
               "formation pathway on graphite anodes in detail.")
        self.assertEqual(clean_feed_abstract(raw), raw)

    def test_trailing_publication_note_removed(self):
        body = "We study the thermal conductivity of amorphous carbon."
        for suffix in (
            " [Phys. Rev. B 114, 175108] Published Fri Sep 04, 2026",
            " [Phys. Rev. Materials 10, 041002]",
            " Published Wed Sep 01, 2026",
        ):
            out = clean_feed_abstract(body + suffix)
            self.assertEqual(out, body, suffix)

    def test_empty_and_whitespace(self):
        self.assertEqual(clean_feed_abstract(""), "")
        self.assertEqual(clean_feed_abstract("   \n  "), "")

    def test_author_prefix_with_date_filter_too_short(self):
        # 清洗后过短的内容不应被当成有效摘要（fetcher 侧另有 len>100 门槛）
        raw = "Author(s): A. SmithWe report data."
        out = clean_feed_abstract(raw)
        self.assertTrue(out.startswith("We report"))
        self.assertLess(len(out), 30)


class LooksTruncatedAbstractTests(unittest.TestCase):
    def test_ellipsis_endings(self):
        for t in ("trails off with...", "trails off with…", "ends like. . ."):
            self.assertTrue(oa.looks_truncated_abstract(t), t)

    def test_short_without_terminal_punctuation(self):
        self.assertTrue(oa.looks_truncated_abstract("short fragment no period"))

    def test_empty_is_truncated(self):
        self.assertTrue(oa.looks_truncated_abstract(""))
        self.assertTrue(oa.looks_truncated_abstract(None))

    def test_complete_sentence_not_truncated(self):
        t = ("A complete abstract that ends with a proper full sentence. "
             "It contains methodology, results, and conclusions.")
        self.assertFalse(oa.looks_truncated_abstract(t), t)

    def test_broken_last_word(self):
        # APS 截断特征：末词被硬切
        t = "Abstract text describing the simulation " + "word " * 20 + "dynam"
        self.assertTrue(oa.looks_truncated_abstract(t))

    def test_short_english_abstract_with_period_not_truncated(self):
        # 误报代价低但需知道行为：短而有句号的正常摘要不被判截断
        self.assertFalse(oa.looks_truncated_abstract(
            "We report a new potential. It works well."))


class TranslationQualityTests(unittest.TestCase):
    def test_short_valid_translation_passes(self):
        # 旧规则（len<120 即失败）会误杀的短译文（门槛 40）
        text = "本文报道了一种新的机器学习原子间势，在界面体系中精度接近DFT，显著降低计算成本。"
        self.assertEqual(
            LLMAnalyzer._translation_quality_error(text, abstract_len=200), "")

    def test_long_translated_abstract_with_terms_passes(self):
        # 含大量英文术语/分子式的正规译文（叙述密度兜底）
        text = ("本文基于 NequIP 框架训练了 Al-Cu 二元体系的 MLIP，"
                "结合 DFT 数据（PBE 泛函，520 eV 截断能）与 active learning，"
                "在 fcc/bcc 相变能量上达到 1.2 meV/atom 的精度，"
                "MD 模拟（NPT 系综，300 K）给出的热膨胀系数与实验一致。")
        self.assertEqual(
            LLMAnalyzer._translation_quality_error(text, abstract_len=800), "")

    def test_english_echo_fails(self):
        # deepseek-flash 偶发回显英文原文：必须按失败处理（中文字符数不足）
        text = ("We report the synthesis of novel perovskite materials "
                "with improved stability and efficiency for applications.")
        err = LLMAnalyzer._translation_quality_error(text, abstract_len=800)
        self.assertTrue(err, err)
        self.assertIn("cjk_chars=", err)  # 全英文 → cjk<15 先触发

    def test_empty_fails(self):
        self.assertEqual(
            LLMAnalyzer._translation_quality_error("", 800), "empty")

    def test_too_short_fails_with_length_reason(self):
        err = LLMAnalyzer._translation_quality_error("太短。", abstract_len=800)
        self.assertIn("len=", err)

    def test_min_len_scales_with_abstract(self):
        medium = "简短摘要的合格译文，叙述完整且信息充分。" * 2  # ~40 字
        # 短摘要（<300 字）门槛 40，应通过
        self.assertEqual(
            LLMAnalyzer._translation_quality_error(medium, abstract_len=150), "")
        # 长摘要门槛 80，同样文本应失败
        err = LLMAnalyzer._translation_quality_error(medium, abstract_len=1000)
        self.assertIn("len=", err)


class DedupAbstractTests(unittest.TestCase):
    def test_prefers_complete_openalex(self):
        good = ("A complete abstract sentence. " * 10).strip()
        with patch.object(oa, "get_openalex_abstract", return_value=good), \
             patch.object(oa, "get_crossref_abstract", return_value="") as xref:
            self.assertEqual(oa.get_dedup_abstract("10.1/x", email="a@b.c"), good)
            xref.assert_not_called()

    def test_falls_back_to_crossref_when_truncated(self):
        truncated = "We report partial results..."
        complete = ("A complete abstract from Crossref. " * 10).strip()
        with patch.object(oa, "get_openalex_abstract", return_value=truncated), \
             patch.object(oa, "get_crossref_abstract", return_value=complete):
            self.assertEqual(oa.get_dedup_abstract("10.1/x"), complete)

    def test_keeps_openalex_when_crossref_not_longer(self):
        truncated = "Partial openalex abstract..."
        shorter = "Shorter crossref text."
        with patch.object(oa, "get_openalex_abstract", return_value=truncated), \
             patch.object(oa, "get_crossref_abstract", return_value=shorter):
            self.assertEqual(oa.get_dedup_abstract("10.1/x"), truncated)

    def test_both_empty(self):
        with patch.object(oa, "get_openalex_abstract", return_value=""), \
             patch.object(oa, "get_crossref_abstract", return_value=""):
            self.assertEqual(oa.get_dedup_abstract("10.1/x"), "")


if __name__ == "__main__":
    unittest.main()
