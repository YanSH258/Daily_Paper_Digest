"""按 DOI / arXiv 编号补全元数据的回归测试：手动添加与解读前摘要补全。

全部使用 mock，不联网：
- OpenAlex 命中时不再问 Crossref；
- OpenAlex 没收录（新发表文献常见）时回退 Crossref 并补全字段；
- OpenAlex 有元数据但缺摘要时用 Crossref 合并补摘要；
- arXiv 链接/编号走 arXiv 接口（预印本没有 DOI，按 DOI 查必然落空）；
- 两家都没有时返回空元数据，前端提示手填（而不是假装成功）；
- Crossref 单篇的字段规范化（JATS 摘要去标签、标题去 HTML、日期优先级、作者拼接）。
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from integrations import arxiv as arxiv_mod  # noqa: E402
from integrations import crossref as crossref_mod  # noqa: E402
from integrations import openalex as openalex_mod  # noqa: E402
import web_server  # noqa: E402

OPENALEX_WORK = {
    "title": "A cached OpenAlex title",
    "journal": "J. Test Chem.",
    "abstract": "OpenAlex abstract text.",
    "authors": ["Ada Lovelace", "Rosalind Franklin"],
    "pub_date": "2026-09-11",
    "url": "https://doi.org/10.1234/oa",
}
CROSSREF_WORK = {
    "doi": "10.1038/s41467-026-77704-9",
    "title": "Hydride-induced palladium self-diffusion",
    "journal": "Nature Communications",
    "abstract": "Crossref abstract text.",
    "authors": "Raju Lipin, Matthias Vandichel",
    "pub_date": "2026-09-15",
    "url": "https://doi.org/10.1038/s41467-026-77704-9",
    "date_source": "crossref_published-online",
}
ARXIV_WORK = {
    "title": "Machine learning interatomic potentials for solid-state precipitation",
    "journal": "cond-mat.mtrl-sci",
    "abstract": "Machine learning interatomic potentials (MLIPs) are routinely used.",
    "authors": ["Lorenzo Piersante", "Anirudh Raju Natarajan"],
    "pub_date": "2026-01-19",
    "url": "https://arxiv.org/abs/2601.12984v1",
    "doi": "10.1103/qyb1-7j1p",
    "arxiv_id": "2601.12984",
    "date_source": "arxiv_id_lookup",
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
    base_url: https://api.deepseek.com
relevance_threshold: 4
output:
  output_dir: {Path(tmp) / 'output'}
""",
        encoding="utf-8",
    )
    return str(cfg)


class LookupDoiMetadataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = web_server.WebContext(config_path=_make_config(self._tmp.name))

    def _lookup(self, *, openalex=None, crossref=None, openalex_exc=None):
        oa = unittest.mock.patch.object(openalex_mod, "get_work_by_doi",
                                        side_effect=openalex_exc) if openalex_exc else \
            unittest.mock.patch.object(openalex_mod, "get_work_by_doi", return_value=openalex)
        cr = unittest.mock.patch.object(crossref_mod, "work_by_doi", return_value=crossref)
        with oa as oa_mock, cr as cr_mock:
            work, source = web_server._lookup_doi_metadata(self.ctx, "10.1234/x")
        return work, source, oa_mock, cr_mock

    def test_openalex_complete_short_circuits_crossref(self):
        work, source, _oa, cr = self._lookup(openalex=OPENALEX_WORK)
        self.assertEqual(source, "openalex")
        self.assertEqual(work["title"], OPENALEX_WORK["title"])
        cr.assert_not_called()

    def test_falls_back_to_crossref_when_openalex_missing(self):
        """新发表文献常有 Crossref 记录但 OpenAlex 尚未收录。"""
        work, source, _oa, cr = self._lookup(openalex=None, crossref=CROSSREF_WORK)
        self.assertEqual(source, "crossref")
        self.assertEqual(work["title"], CROSSREF_WORK["title"])
        self.assertTrue(work["abstract"])
        cr.assert_called_once()

    def test_merges_crossref_abstract_when_openalex_lacks_it(self):
        partial = dict(OPENALEX_WORK, abstract="")
        work, source, _oa, cr = self._lookup(openalex=partial, crossref=CROSSREF_WORK)
        self.assertEqual(source, "openalex+crossref")
        # 已有字段保留 OpenAlex 的，缺的摘要由 Crossref 补
        self.assertEqual(work["title"], OPENALEX_WORK["title"])
        self.assertEqual(work["abstract"], CROSSREF_WORK["abstract"])
        cr.assert_called_once()

    def test_both_missing_returns_empty(self):
        work, source, _oa, _cr = self._lookup(openalex=None, crossref=None)
        self.assertEqual(work, {})
        self.assertEqual(source, "")

    def test_openalex_exception_still_falls_back(self):
        work, source, _oa, _cr = self._lookup(openalex_exc=RuntimeError("boom"),
                                              crossref=CROSSREF_WORK)
        self.assertEqual(source, "crossref")
        self.assertEqual(work["title"], CROSSREF_WORK["title"])


class ManualAddApiTests(unittest.TestCase):
    """/api/articles/manual 的 dry_run 补全：返回字段与来源必须如实。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = web_server.WebContext(config_path=_make_config(self._tmp.name))

    def _dry_run(self, payload, work, source):
        with unittest.mock.patch.object(web_server, "_lookup_doi_metadata",
                                        return_value=(work, source)):
            return web_server._manual_add_article(self.ctx, payload)

    def test_crossref_fallback_fills_fields(self):
        body, code = self._dry_run({"doi": "https://doi.org/10.1038/s41467-026-77704-9",
                                    "dry_run": True}, CROSSREF_WORK, "crossref")
        self.assertEqual(code, 200)
        self.assertEqual(body["fetched_from"], "crossref")
        self.assertFalse(body["fetched_from_openalex"])
        article = body["article"]
        self.assertEqual(article["doi"], "10.1038/s41467-026-77704-9")  # URL 前缀被规范化
        self.assertEqual(article["title"], CROSSREF_WORK["title"])
        self.assertEqual(article["journal"], "Nature Communications")
        self.assertEqual(article["pub_date"], "2026-09-15")
        self.assertEqual(article["authors"], CROSSREF_WORK["authors"])
        self.assertTrue(article["abstract"])

    def test_openalex_authors_list_joined(self):
        body, _ = self._dry_run({"doi": "10.1234/oa", "dry_run": True}, OPENALEX_WORK, "openalex")
        self.assertTrue(body["fetched_from_openalex"])
        self.assertEqual(body["article"]["authors"], "Ada Lovelace, Rosalind Franklin")

    def test_nothing_found_returns_empty_article(self):
        body, code = self._dry_run({"doi": "10.9999/none", "dry_run": True}, {}, "")
        self.assertEqual(code, 200)
        self.assertEqual(body["fetched_from"], "")
        self.assertEqual(body["article"]["title"], "")

    def test_user_typed_fields_are_not_overwritten(self):
        body, _ = self._dry_run({"doi": "10.1234/oa", "title": "我自己的标题",
                                 "dry_run": True}, OPENALEX_WORK, "openalex")
        self.assertEqual(body["article"]["title"], "我自己的标题")
        self.assertEqual(body["article"]["journal"], OPENALEX_WORK["journal"])

    def test_manual_fields_without_doi_still_accepted(self):
        body, code = self._dry_run({"title": "只有标题", "dry_run": True}, {}, "")
        self.assertEqual(code, 200)
        self.assertEqual(body["article"]["title"], "只有标题")


class ArxivIdParsingTests(unittest.TestCase):
    """编号识别：从链接/编号里取出 arXiv ID，且不能把 DOI 误判成编号。"""

    def test_accepts_common_forms(self):
        for text in ("https://arxiv.org/abs/2601.12984v1",
                     "https://arxiv.org/pdf/2601.12984v1.pdf",
                     "arXiv:2601.12984",
                     "2601.12984v3",
                     "2601.12984",
                     "arxiv.org/abs/2601.12984"):
            with self.subTest(text=text):
                self.assertEqual(arxiv_mod.parse_id(text), "2601.12984")

    def test_accepts_old_style_and_datacite_doi(self):
        self.assertEqual(arxiv_mod.parse_id("math.GT/0309136"), "math.GT/0309136")
        self.assertEqual(arxiv_mod.parse_id("10.48550/arXiv.2601.12984"), "2601.12984")
        self.assertEqual(arxiv_mod.parse_id("https://doi.org/10.48550/arXiv.2601.12984"),
                         "2601.12984")

    def test_does_not_mistake_doi_for_arxiv_id(self):
        # 10.1000/5678.9012 的数字片段长得像 arXiv 编号，不能误判
        for text in ("10.1000/5678.9012",
                     "10.1038/s41467-026-77704-9",
                     "https://doi.org/10.1103/PhysRevB.110.014301",
                     "cond-mat.mtrl-sci",
                     ""):
            with self.subTest(text=text):
                self.assertEqual(arxiv_mod.parse_id(text), "")


class ArxivLookupTests(unittest.TestCase):
    """arXiv 单篇查询：解析 Atom 条目并如实标注来源。"""

    ENTRY = {
        "title": "Machine learning interatomic potentials for solid-state precipitation",
        "summary": "Machine learning interatomic potentials (MLIPs) are routinely used.",
        "published": "2026-01-19T11:58:30Z",
        "updated": "2026-01-19T11:58:30Z",
        "link": "https://arxiv.org/abs/2601.12984v1",
        "id": "http://arxiv.org/abs/2601.12984v1",
        "arxiv_primary_category": {"term": "cond-mat.mtrl-sci"},
        "arxiv_doi": "10.1103/qyb1-7j1p",
        "authors": [{"name": "Lorenzo Piersante"}, {"name": "Anirudh Raju Natarajan"}],
    }

    def _feed(self, entries):
        return unittest.mock.Mock(entries=entries)

    def test_normalizes_entry(self):
        with unittest.mock.patch.object(arxiv_mod, "fetch_feed",
                                        return_value=self._feed([self.ENTRY])) as fetch:
            work = arxiv_mod.lookup_by_id("https://arxiv.org/abs/2601.12984v1")
        self.assertEqual(work["title"], self.ENTRY["title"])
        self.assertEqual(work["journal"], "cond-mat.mtrl-sci")
        self.assertEqual(work["pub_date"], "2026-01-19")
        self.assertEqual(work["doi"], "10.1103/qyb1-7j1p")
        self.assertEqual(work["arxiv_id"], "2601.12984")
        self.assertEqual(work["authors"], ["Lorenzo Piersante", "Anirudh Raju Natarajan"])
        self.assertEqual(work["date_source"], "arxiv_id_lookup")
        self.assertIn("id_list=2601.12984", fetch.call_args[0][0])

    def test_error_entry_returns_none(self):
        bad = dict(self.ENTRY, title="Error", summary="")
        with unittest.mock.patch.object(arxiv_mod, "fetch_feed",
                                        return_value=self._feed([bad])):
            self.assertIsNone(arxiv_mod.lookup_by_id("2601.99999"))

    def test_empty_feed_returns_none(self):
        with unittest.mock.patch.object(arxiv_mod, "fetch_feed",
                                        return_value=self._feed([])):
            self.assertIsNone(arxiv_mod.lookup_by_id("2601.99999"))

    def test_non_arxiv_input_makes_no_request(self):
        with unittest.mock.patch.object(arxiv_mod, "fetch_feed") as fetch:
            self.assertIsNone(arxiv_mod.lookup_by_id("10.1038/s41467-026-77704-9"))
        fetch.assert_not_called()


class ManualAddArxivTests(unittest.TestCase):
    """手动添加 arXiv 预印本：不把链接当成 DOI，字段由 arXiv 补全。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = web_server.WebContext(config_path=_make_config(self._tmp.name))

    def _add(self, payload, work):
        with unittest.mock.patch.object(web_server, "_lookup_arxiv_metadata",
                                        return_value=(work, "arxiv" if work else "")):
            return web_server._manual_add_article(self.ctx, payload)

    def test_arxiv_url_is_not_stored_as_doi(self):
        body, code = self._add({"doi": "https://arxiv.org/abs/2601.12984v1", "dry_run": True},
                               ARXIV_WORK)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["fetched_from"], "arxiv")
        article = body["article"]
        self.assertEqual(article["doi"], "10.1103/qyb1-7j1p")  # arXiv 记录里的已发表 DOI
        self.assertEqual(article["title"], ARXIV_WORK["title"])
        self.assertEqual(article["url"], ARXIV_WORK["url"])
        self.assertEqual(article["pub_date"], "2026-01-19")
        self.assertEqual(article["authors"], "Lorenzo Piersante, Anirudh Raju Natarajan")
        self.assertTrue(article["abstract"])

    def test_bare_id_without_doi_field_still_adds(self):
        body, code = self._add({"doi": "2601.12984", "dry_run": True}, ARXIV_WORK)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_arxiv_miss_reports_empty(self):
        body, code = self._add({"doi": "2601.99999", "dry_run": True}, {})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["fetched_from"], "")
        self.assertEqual(body["article"]["title"], "")

    def test_write_path_persists_arxiv_article(self):
        body, code = self._add({"doi": "https://arxiv.org/abs/2601.12984v1"}, ARXIV_WORK)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        conn = self.ctx.connect_db()
        try:
            row = conn.execute("SELECT title, journal, pub_date, url, doi FROM articles"
                               ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], ARXIV_WORK["title"])
        self.assertEqual(row[1], "cond-mat.mtrl-sci")
        self.assertEqual(row[2], "2026-01-19")
        self.assertEqual(row[3], ARXIV_WORK["url"])
        self.assertEqual(row[4], "10.1103/qyb1-7j1p")


class ManualAddWriteTests(unittest.TestCase):
    """真实写入路径：只给 DOI 时补全并入库，响应必须如实报告成功。

    这条路径曾经在写入后被未定义的变量打断，返回"添加失败"而数据其实已写入。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = web_server.WebContext(config_path=_make_config(self._tmp.name))

    def _add(self, payload, work, source):
        with unittest.mock.patch.object(web_server, "_lookup_doi_metadata",
                                        return_value=(work, source)):
            return web_server._manual_add_article(self.ctx, payload)

    def _rows(self):
        conn = self.ctx.connect_db()
        try:
            return conn.execute(
                "SELECT id, doi, title, journal, pub_date, authors, abstract FROM articles"
            ).fetchall()
        finally:
            conn.close()

    def test_doi_only_add_succeeds_and_reports_source(self):
        body, code = self._add({"doi": "10.1038/s41467-026-77704-9"}, CROSSREF_WORK, "crossref")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["fetched_from"], "crossref")
        self.assertFalse(body["fetched_from_openalex"])
        self.assertIsInstance(body["id"], int)

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[1], "10.1038/s41467-026-77704-9")
        self.assertEqual(row[2], CROSSREF_WORK["title"])
        self.assertEqual(row[3], "Nature Communications")
        self.assertEqual(row[4], "2026-09-15")
        self.assertEqual(row[6], CROSSREF_WORK["abstract"])
        self.assertTrue(row[5])

    def test_metric_lookup_failure_does_not_fake_failure(self):
        with unittest.mock.patch.object(self.ctx.db, "get_journal_metric",
                                        side_effect=RuntimeError("metric db down")):
            body, code = self._add({"doi": "10.1234/oa"}, OPENALEX_WORK, "openalex")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertIsNone(body["impact_factor"])

    def test_duplicate_add_is_reported_not_duplicated(self):
        first, code = self._add({"doi": "10.1234/oa"}, OPENALEX_WORK, "openalex")
        self.assertEqual(code, 200)
        second, code = self._add({"doi": "10.1234/oa"}, OPENALEX_WORK, "openalex")
        self.assertEqual(code, 200)
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self._rows()), 1)


class CrossrefWorkByDoiTests(unittest.TestCase):
    """Crossref 单篇规范化：只测内部逻辑，不联网。"""

    JATS = ("<jats:title>Abstract</jats:title><jats:p>Reaction conditions affect "
            "the electrocatalyst surface.</jats:p>")

    def _item(self, **over):
        item = {
            "DOI": "10.1038/s41467-026-77704-9",
            "title": ["Hydride-induced <i>palladium</i> self-diffusion"],
            "container-title": ["Nature Communications"],
            "author": [{"given": "Raju", "family": "Lipin"}, {"name": "Consortium"}],
            "published-online": {"date-parts": [[2026, 9, 15]]},
            "published-print": {"date-parts": [[2026, 10, 1]]},
            "abstract": self.JATS,
        }
        item.update(over)
        return item

    def test_normalizes_fields(self):
        article = crossref_mod._normalize_item(self._item())
        self.assertEqual(article["title"], "Hydride-induced palladium self-diffusion")
        self.assertEqual(article["journal"], "Nature Communications")
        self.assertEqual(article["pub_date"], "2026-09-15")  # 优先 published-online
        self.assertEqual(article["date_source"], "crossref_published-online")
        self.assertEqual(article["url"], "https://doi.org/10.1038/s41467-026-77704-9")
        self.assertEqual(article["authors"], "Raju Lipin, Consortium")
        self.assertNotIn("jats", article["abstract"])

    def test_strips_literal_abstract_label(self):
        article = crossref_mod._normalize_item(self._item())
        self.assertTrue(article["abstract"].startswith("Reaction conditions"))
        self.assertNotIn("<", article["abstract"])

    def test_missing_date_reported_as_missing(self):
        article = crossref_mod._normalize_item(
            self._item(**{"published-online": None, "published-print": None}))
        self.assertEqual(article["pub_date"], "")
        self.assertEqual(article["date_source"], "missing")

    def test_work_by_doi_returns_none_on_404(self):
        resp = unittest.mock.Mock(status_code=404)
        with unittest.mock.patch.object(crossref_mod.requests, "get", return_value=resp):
            self.assertIsNone(crossref_mod.work_by_doi("10.9999/nope"))
        resp.close.assert_called_once()

    def test_work_by_doi_returns_normalized_record(self):
        resp = unittest.mock.Mock(status_code=200)
        resp.json.return_value = {"message": self._item()}
        with unittest.mock.patch.object(crossref_mod.requests, "get", return_value=resp) as get:
            article = crossref_mod.work_by_doi("10.1038/s41467-026-77704-9")
        self.assertEqual(article["discovered_via"], "crossref_doi")
        self.assertEqual(article["doi"], "10.1038/s41467-026-77704-9")
        self.assertIn("/works/10.1038/s41467-026-77704-9", get.call_args[0][0])

    def test_work_by_doi_rejects_empty_doi(self):
        self.assertIsNone(crossref_mod.work_by_doi(""))


if __name__ == "__main__":
    unittest.main()
