"""Display cleanup keeps substantive claims and provenance in stored snapshots."""
from pathlib import Path
import tempfile
import unittest

from core.notifier import Notifier, _report_analysis_sections


class ReportWordingTests(unittest.TestCase):
    def test_empty_template_sections_removed_without_losing_claims(self):
        raw = '''### 0. 摘要翻译
保留：DFT 数据用于训练机器学习势。
---
### 1. 方法设计
因未获取到全文，摘要中无此信息。
### 2. 实验表现
因未获取到全文，摘要中无此信息。（摘要中未提及具体结果数据或性能指标。）
### 6. 总结
**a) 一句话核心思想**（基于摘要概括，≤20字）
保留这句话。
**b) 速记版 Pipeline**：因未获取到全文，摘要中无此信息。
> ⚠️ **[系统提示：AI 解读因达到输出长度上限被截断。请在 config.yaml 中调大 max_tokens（当前为 8192）。]**
'''
        sections, truncated = _report_analysis_sections(raw)
        self.assertEqual(sections, [('摘要翻译', '保留：DFT 数据用于训练机器学习势。'), ('总结', '保留这句话。')])
        self.assertTrue(truncated)

    def test_caveats_and_source_truncation_remain(self):
        raw = '### 1. 方法动机\n仅基于摘要，结论尚未验证。原文截断。\n某段重复出现摘要中无此信息，但这里有实质说明。'
        sections, truncated = _report_analysis_sections(raw)
        self.assertIn('仅基于摘要，结论尚未验证。原文截断。', sections[0][1])
        self.assertIn('这里有实质说明', sections[0][1])
        self.assertFalse(truncated)

    def test_render_omits_internal_labels_and_empty_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            notifier = Notifier({'output': {'output_dir': tmp}})
            analysis = '### 1. 方法设计\n因未获取到全文，摘要中无此信息。'
            payload = {'digest_date': '2026-09-11', 'version': 2, 'content_hash': 'privatehash',
                       'stats': {'candidates': 1, 'selected': 1, 'by_category': {'mlip': 1}},
                       'items': [{'article_id': 4, 'rank': 1, 'category': 'mlip', 'reason': '',
                                  'snapshot': {'title': 'Test paper', 'analysis': analysis,
                                               'abstract': 'Original abstract', 'evidence_level': 'ABSTRACT'}}]}
            artifacts = notifier.render_digest_version(payload)
            md = Path(next(a['path'] for a in artifacts if a['format'] == 'markdown')).read_text()
            for noise in ('未记录', '文章 ID：', '排名：', '模型/规则', '证据等级：', '综合分：',
                          '因未获取到全文', '### 方法设计', 'privatehash', '固定存档'):
                self.assertNotIn(noise, md)
            self.assertIn('仅有摘要', md)
            self.assertIn('现有材料不足以提供解读', md)
            self.assertEqual(payload['items'][0]['snapshot']['analysis'], analysis)
            self.assertIn('id="paper-4-rank-1"', md)
