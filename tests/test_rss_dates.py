"""RSS date regressions through mocked HTTP and the real feedparser pipeline."""
import unittest
from unittest.mock import Mock, patch

import feedparser

from core.fetcher import JournalFetcher
from fetchers.feed_dates import unfetched_entries_may_be_in_window
from processing import admission_decision


NATURE_URL = "https://www.nature.com/nmat.rss"
NATURE_FEED_URL = "http://feeds.nature.com/nature/rss/current"
RUN_DATE = "2026-09-15"


def rdf_item(dates="", key="paper"):
    url = f"https://www.nature.com/articles/{key}"
    return (f'<item rdf:about="{url}"><title>Paper {key}</title>'
            f'<link>{url}</link>{dates}</item>')


def rdf_feed(items, channel_date=""):
    return f'''<?xml version="1.0" encoding="utf-8"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns="http://purl.org/rss/1.0/"
             xmlns:dc="http://purl.org/dc/elements/1.1/"
             xmlns:dcterms="http://purl.org/dc/terms/"
             xmlns:atom="http://www.w3.org/2005/Atom">
      <channel rdf:about="https://www.nature.com/nmat/">
        <title>Nature Materials</title><link>https://www.nature.com/nmat/</link>
        <description>Latest articles</description>{channel_date}
      </channel>{items}
    </rdf:RDF>'''.encode()


def atom_feed(dates):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"
                    xmlns:dc="http://purl.org/dc/elements/1.1/"
                    xmlns:dcterms="http://purl.org/dc/terms/">
      <title>arXiv</title><updated>2026-09-15T00:00:00Z</updated>
      <entry><id>https://arxiv.org/abs/2609.00001</id>
        <title>First submitted paper</title>
        <link href="https://arxiv.org/abs/2609.00001"/>{dates}
      </entry></feed>'''.encode()


class RSSDateTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "_run_date": RUN_DATE,
            "scheduler": {"timezone": "Asia/Shanghai"},
            "fetcher": {"date_filter_days": 3, "request_timeout": 7,
                        "retry_times": 1, "use_browser": False},
        }

    def fetch(self, xml, *, url=NATURE_URL, response_url=None,
              publisher="Nature", test_feed=False):
        fetcher = JournalFetcher(self.config)
        self.addCleanup(fetcher.close)
        response = Mock(content=xml, url=response_url or url)
        with patch("core.fetcher.requests.Session.get", return_value=response) as get:
            if test_feed:
                result = fetcher.test_feed(url, publisher)
            else:
                result = fetcher._fetch_journal(
                    {"name": "Journal", "rss": url, "publisher": publisher})
        # Inspecting dates must not add a fetch or change request timeouts.
        get.assert_called_once_with(url, timeout=7)
        response.raise_for_status.assert_called_once_with()
        return result

    def decision(self, article):
        return admission_decision(article, RUN_DATE, self.config)

    def test_nature_rdf_dc_date_has_publication_provenance(self):
        xml = rdf_feed(rdf_item("<dc:date>2026-09-15</dc:date>"))
        for publisher in ("Nature", "DEFAULT"):
            with self.subTest(publisher=publisher):
                rows = self.fetch(xml, publisher=publisher)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["pub_date"], "2026-09-15")
                self.assertEqual(rows[0]["pub_date_source"], "rss_dc_date")
                decision = self.decision(rows[0])
                self.assertEqual(decision["decision"], "eligible")
                self.assertEqual(decision["date_source"], "rss_dc_date")

    def test_raw_partial_invalid_and_future_dates_are_not_repaired(self):
        cases = (
            ("", "needs_date"),
            ("2026", "needs_date"),
            ("2026-09", "needs_date"),
            ("2026-02-30", "invalid_date"),
            ("2026-02-30T00:00:00Z", "invalid_date"),
            ("2026-13-01", "invalid_date"),
            ("2026-09-15T25:00:00Z", "invalid_date"),
            ("30 Feb 2026 00:00:00 GMT", "invalid_date"),
            ("not-a-date", "invalid_date"),
            ("2026-09-16", "invalid_date"),
        )
        for tag, source in (("dc:date", "rss_dc_date"), ("pubDate", "rss_published")):
            for raw, expected in cases:
                with self.subTest(tag=tag, raw=raw):
                    article, = self.fetch(rdf_feed(rdf_item(f"<{tag}>{raw}</{tag}>")))
                    self.assertEqual(article["pub_date"], raw)
                    self.assertEqual(article["pub_date_source"], source)
                    decision = self.decision(article)
                    self.assertEqual(decision["decision"], expected)
                    if raw == "2026-09-16":
                        self.assertEqual(decision["reason"], "future_date")

    def test_timezone_and_inclusive_calendar_window_boundaries(self):
        cases = (
            ("Asia/Shanghai", "2026-09-12T15:59:59Z", "outside_window", "2026-09-12"),
            ("Asia/Shanghai", "2026-09-12T16:00:00Z", "eligible", "2026-09-13"),
            ("Asia/Shanghai", "2026-09-15T15:59:59Z", "eligible", "2026-09-15"),
            ("Asia/Shanghai", "2026-09-15T16:00:00Z", "invalid_date", "2026-09-16"),
            ("Asia/Shanghai", "2026-09-13T00:00:00+08:00", "eligible", "2026-09-13"),
            ("America/Los_Angeles", "2026-09-13T06:59:59Z", "outside_window", "2026-09-12"),
            ("America/Los_Angeles", "2026-09-13T07:00:00Z", "eligible", "2026-09-13"),
            ("America/Los_Angeles", "2026-09-16T06:59:59Z", "eligible", "2026-09-15"),
            ("America/Los_Angeles", "2026-09-16T07:00:00Z", "invalid_date", "2026-09-16"),
        )
        for tag in ("dc:date", "pubDate"):
            for zone, raw, expected, day in cases:
                with self.subTest(tag=tag, zone=zone, raw=raw):
                    self.config["scheduler"]["timezone"] = zone
                    article, = self.fetch(rdf_feed(rdf_item(f"<{tag}>{raw}</{tag}>")))
                    decision = self.decision(article)
                    self.assertEqual(decision["decision"], expected)
                    self.assertEqual(decision["publication_date"], day)
                    self.assertEqual(decision["window_start"], "2026-09-13")
                    self.assertEqual(decision["window_end"], RUN_DATE)

    def test_date_only_and_timezone_free_times_do_not_acquire_utc(self):
        self.config["scheduler"]["timezone"] = "America/Los_Angeles"
        for tag in ("dc:date", "pubDate"):
            for raw in ("2026-09-13", "2026-09-13T00:00:00"):
                with self.subTest(tag=tag, raw=raw):
                    article, = self.fetch(rdf_feed(rdf_item(f"<{tag}>{raw}</{tag}>")))
                    self.assertEqual(article["pub_date"], raw)
                    self.assertEqual(self.decision(article)["decision"], "eligible")
                    self.assertEqual(self.decision(article)["publication_date"], "2026-09-13")

    def test_rfc_published_is_validated_and_keeps_timezone(self):
        article, = self.fetch(rdf_feed(rdf_item(
            "<pubDate>Sun, 13 Sep 2026 00:00:00 +0800</pubDate>")))
        self.assertEqual(article["pub_date"], "2026-09-13T00:00:00+08:00")
        self.assertEqual(article["pub_date_source"], "rss_published")
        self.assertEqual(self.decision(article)["decision"], "eligible")

    def test_published_precedence_including_invalid_or_partial_values(self):
        for raw, expected in (("2026-09-14", "eligible"), ("2020-01-01", "outside_window"),
                              ("2026-09", "needs_date"), ("2026-02-30", "invalid_date"),
                              ("", "needs_date"), ("2026-09-16", "invalid_date")):
            for published_first in (False, True):
                with self.subTest(raw=raw, published_first=published_first):
                    fields = [f"<pubDate>{raw}</pubDate>", "<dc:date>2026-09-15</dc:date>"]
                    if not published_first:
                        fields.reverse()
                    article, = self.fetch(rdf_feed(rdf_item("".join(fields))))
                    self.assertEqual(article["pub_date"], raw)
                    self.assertEqual(article["pub_date_source"], "rss_published")
                    self.assertEqual(self.decision(article)["decision"], expected)

    def test_atom_updated_never_replaces_first_publication(self):
        article, = self.fetch(atom_feed(
            "<published>2020-01-01T00:00:00Z</published>"
            "<updated>2026-09-15T00:00:00Z</updated>"),
            url="https://export.arxiv.org/api/query", publisher="arXiv")
        self.assertEqual(article["pub_date_source"], "rss_published")
        self.assertEqual(self.decision(article)["publication_date"], "2020-01-01")
        self.assertEqual(self.decision(article)["decision"], "outside_window")

    def test_atom_updates_modified_and_dc_dates_are_not_publication_fallbacks(self):
        for url, publisher in (("https://export.arxiv.org/api/query", "arXiv"),
                               (NATURE_URL, "Nature")):
            for dates in ("<updated>2026-09-15T00:00:00Z</updated>",
                          "<dcterms:modified>2026-09-15</dcterms:modified>",
                          "<dc:date>2026-09-15</dc:date>", ""):
                with self.subTest(url=url, dates=dates):
                    article, = self.fetch(atom_feed(dates), url=url, publisher=publisher)
                    self.assertEqual(article["pub_date"], "")
                    self.assertEqual(article["pub_date_source"], "missing")
                    self.assertTrue(article["quarantine"])
                    self.assertEqual(self.decision(article)["decision"], "needs_date")

    def test_rdf_modified_and_channel_date_cannot_date_an_item(self):
        for dates in ("<dcterms:modified>2026-09-15</dcterms:modified>",
                      "<atom:updated>2026-09-15T00:00:00Z</atom:updated>",
                      '<other:date xmlns:other="https://example.test/dc/">2026-09-15</other:date>',
                      ""):
            with self.subTest(dates=dates):
                xml = rdf_feed(rdf_item(dates), channel_date="<dc:date>2026-09-15</dc:date>")
                article, = self.fetch(xml)
                self.assertEqual(article["pub_date"], "")
                self.assertEqual(article["pub_date_source"], "missing")
                self.assertEqual(self.decision(article)["decision"], "needs_date")

    def test_rss2_dc_date_is_not_an_allowed_fallback(self):
        xml = b'''<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
        <channel><title>Nature</title><item><title>Paper</title>
        <link>https://www.nature.com/articles/paper</link>
        <dc:date>2026-09-15</dc:date></item></channel></rss>'''
        article, = self.fetch(xml)
        self.assertEqual(article["pub_date"], "")
        self.assertEqual(self.decision(article)["decision"], "needs_date")

    def test_only_official_nature_requests_and_responses_are_trusted(self):
        xml = rdf_feed(rdf_item("<dc:date>2026-09-15</dc:date>"))
        for url, response_url, trusted in (
            (NATURE_URL, NATURE_URL, True),
            (NATURE_FEED_URL, NATURE_FEED_URL, True),
            ("https://example.test/nmat.rss", "https://example.test/nmat.rss", False),
            ("https://www.nature.com.example.test/nmat.rss", None, False),
            ("https://www.nature.com@example.test/nmat.rss", None, False),
            (NATURE_URL, "https://example.test/nmat.rss", False),
            ("https://example.test/nmat.rss", NATURE_URL, False),
            (NATURE_FEED_URL, "https://feeds.nature.com.example.test/nmat.rss", False),
        ):
            with self.subTest(url=url, response_url=response_url):
                article, = self.fetch(xml, url=url, response_url=response_url)
                self.assertEqual(article["pub_date"], "2026-09-15" if trusted else "")
                self.assertEqual(article["pub_date_source"],
                                 "rss_dc_date" if trusted else "missing")

    def test_dc_date_is_not_overwritten_by_modified_or_updated(self):
        for other in ("<dcterms:modified>2026-09-16</dcterms:modified>",
                      "<atom:updated>2026-09-16T00:00:00Z</atom:updated>"):
            for dc_first in (False, True):
                with self.subTest(other=other, dc_first=dc_first):
                    fields = ["<dc:date>2026-09-13</dc:date>", other]
                    if not dc_first:
                        fields.reverse()
                    article, = self.fetch(rdf_feed(rdf_item("".join(fields))))
                    self.assertEqual(article["pub_date"], "2026-09-13")
                    self.assertEqual(article["pub_date_source"], "rss_dc_date")
                    self.assertEqual(self.decision(article)["decision"], "eligible")

    def test_dates_match_item_identity_even_if_parsed_order_changes(self):
        xml = rdf_feed(
            rdf_item("<dc:date>2026-09-13</dc:date>", "first")
            + rdf_item("", "undated")
            + rdf_item("<dc:date>2026-09-15</dc:date>", "last"))
        parse = feedparser.parse

        def reversed_entries(content):
            feed = parse(content)
            feed.entries.reverse()
            return feed

        with patch("core.fetcher.feedparser.parse", side_effect=reversed_entries):
            articles = self.fetch(xml)
        self.assertEqual({a["title"]: a["pub_date"] for a in articles}, {
            "Paper first": "2026-09-13", "Paper undated": "", "Paper last": "2026-09-15"})

    def test_ambiguous_duplicate_items_or_dates_do_not_enable_fallback(self):
        for items in (
            rdf_item("<dc:date>2026-09-13</dc:date><dc:date>2026-09-15</dc:date>"),
            rdf_item("<dc:date>2026-09-13</dc:date>") + rdf_item(""),
        ):
            with self.subTest(items=items):
                articles = self.fetch(rdf_feed(items))
                self.assertTrue(articles)
                self.assertTrue(all(a["pub_date"] == "" for a in articles))

    def test_malformed_xml_and_entity_declarations_do_not_enable_fallback(self):
        xml = rdf_feed(rdf_item("<dc:date>2026-09-15</dc:date>"))
        external = xml.replace(b'?>', b'?>\n<!DOCTYPE rdf:RDF SYSTEM "https://example.invalid/feed.dtd">', 1)
        internal = xml.replace(b'?>', b'?>\n<!DOCTYPE rdf:RDF [<!ENTITY when "2026-09-15">]>', 1)
        internal = internal.replace(b"<dc:date>2026-09-15", b"<dc:date>&when;")
        for content in (xml.replace(b"</rdf:RDF>", b""), external, internal):
            with self.subTest(content=content):
                article, = self.fetch(content)
                self.assertEqual(article["pub_date"], "")
                self.assertEqual(article["pub_date_source"], "missing")

    def test_parsed_only_published_fixture_remains_supported(self):
        entry = feedparser.FeedParserDict(
            title="Paper", published_parsed=(2026, 9, 12, 16, 0, 0, 5, 255, 0))
        fetcher = JournalFetcher(self.config)
        self.addCleanup(fetcher.close)
        article = fetcher._parse_entry(entry, "Journal", "Nature")
        self.assertEqual(article["pub_date"], "2026-09-12T16:00:00Z")
        self.assertEqual(article["pub_date_source"], "rss_published")
        self.assertEqual(self.decision(article)["decision"], "eligible")
        entry.pop("published_parsed")
        entry["updated_parsed"] = (2026, 9, 15, 0, 0, 0, 1, 258, 0)
        article = fetcher._parse_entry(entry, "Journal", "Nature")
        self.assertEqual(article["pub_date"], "")
        self.assertEqual(article["pub_date_source"], "missing")

    def test_test_feed_counts_only_dates_eligible_for_collection(self):
        self.config["scheduler"]["timezone"] = "America/Los_Angeles"
        items = "".join(rdf_item(fields, str(i)) for i, fields in enumerate((
            "<dc:date>2026-09-13</dc:date>",
            "<dc:date>2026-09-16T06:59:59Z</dc:date>",
            "<pubDate>Sun, 13 Sep 2026 00:00:00 -0700</pubDate>",
            "<dc:date>2026-09-16T07:00:00Z</dc:date>",
            "<dc:date>2026-09-12</dc:date>",
            "<dc:date>2026-09</dc:date>",
            "<dc:date>2026-02-30</dc:date>",
            "<dcterms:modified>2026-09-15</dcterms:modified>",
            "",
        )))
        xml = rdf_feed(items)
        articles = self.fetch(xml)
        expected = sum(self.decision(a)["decision"] == "eligible" for a in articles)
        self.assertEqual(expected, 3)
        result = self.fetch(xml, test_feed=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], len(articles))
        self.assertEqual(result["window_count"], expected)


class FeedTruncationTests(unittest.TestCase):
    """截断只取决于窗口内条目是否被覆盖，而不是讯息流历史存量。"""

    def setUp(self):
        from datetime import date
        self.window_date = date(2026, 9, 23)

    def entries(self, dates):
        # 新增到旧的顺序无关紧要：判定会检查超出上限的每一条
        return [{"published": d} for d in dates]

    def test_backlog_beyond_cap_all_outside_window_is_not_truncated(self):
        old = [f"2026-09-0{n}" for n in range(1, 10)]
        self.assertFalse(unfetched_entries_may_be_in_window(
            self.entries(["2026-09-23", "2026-09-22", "2026-09-21"] + old), 5, self.window_date))

    def test_in_window_entry_beyond_cap_is_truncated(self):
        entries = self.entries(["2026-09-23", "2026-09-22", "2026-09-20",
                                "2026-09-19", "2026-09-18", "2026-09-21"])
        self.assertTrue(unfetched_entries_may_be_in_window(entries, 5, self.window_date))

    def test_entries_within_cap_are_never_truncated(self):
        self.assertFalse(unfetched_entries_may_be_in_window(
            self.entries(["2026-09-23", "2026-09-22"]), 5, self.window_date))

    def test_missing_or_unparseable_date_beyond_cap_is_conservatively_truncated(self):
        entries = self.entries(["2026-09-23", "2026-09-22", "2026-08-01"])
        entries.append({"title": "no date at all"})
        self.assertTrue(unfetched_entries_may_be_in_window(entries, 2, self.window_date))
        entries_with_bad_date = self.entries(["2026-09-23", "2026-09-22", "2026-02-30"])
        self.assertTrue(unfetched_entries_may_be_in_window(entries_with_bad_date, 2, self.window_date))

    def test_future_dates_beyond_cap_do_not_force_truncation(self):
        entries = self.entries(["2026-09-23", "2026-09-22", "2026-09-30"])
        self.assertFalse(unfetched_entries_may_be_in_window(entries, 2, self.window_date))

    def test_window_boundaries_are_inclusive(self):
        entries = self.entries(["2026-09-23", "2026-09-22", "2026-09-21"])
        self.assertTrue(unfetched_entries_may_be_in_window(entries, 2, self.window_date))
        self.assertFalse(unfetched_entries_may_be_in_window(
            self.entries(["2026-09-23", "2026-09-22", "2026-09-20"]), 2, self.window_date))


if __name__ == "__main__":
    unittest.main()
