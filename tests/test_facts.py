"""Offline tests for DigestSmith's facts layer.

The parsers are tested against recorded payloads so the suite runs with
no network and no model: the agent is only as honest as this layer, so
this layer is the one with regression tests.

Run:  python -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest_agent import parse_arxiv, parse_hn, slugify  # noqa: E402

HN_PAYLOAD = json.dumps(
    {
        "hits": [
            {
                "title": "Decentralized inference crosses the chasm",
                "url": "https://example.com/post",
                "points": 128,
                "num_comments": 64,
                "created_at": "2026-11-20T10:00:00.000Z",
                "objectID": "1",
            },
            {
                "title": "Ask HN: who runs local models?",
                "url": None,
                "points": 12,
                "num_comments": 40,
                "created_at": "2026-11-21T09:00:00.000Z",
                "objectID": "2",
            },
            {"title": "", "url": "https://example.com/empty", "objectID": "3"},
        ]
    }
).encode()

ARXIV_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2511.00001v1</id>
    <title>  Scheduling   Inference Across
       Untrusted Nodes </title>
    <summary>We study scheduling.   It works well.</summary>
    <published>2026-11-19T00:00:00Z</published>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <link rel="alternate" href="https://arxiv.org/abs/2511.00001v1"/>
  </entry>
</feed>"""


class TestHackerNewsParser(unittest.TestCase):
    def test_parses_and_sorts_by_points(self):
        items = parse_hn(HN_PAYLOAD)
        self.assertEqual(len(items), 2)  # empty title dropped
        self.assertEqual(items[0]["points"], 128)
        self.assertEqual(items[0]["source"], "Hacker News")
        self.assertEqual(items[0]["date"], "2026-11-20")

    def test_missing_url_falls_back_to_hn_item_page(self):
        items = parse_hn(HN_PAYLOAD)
        self.assertEqual(
            items[1]["url"], "https://news.ycombinator.com/item?id=2"
        )


class TestArxivParser(unittest.TestCase):
    def test_parses_entry(self):
        items = parse_arxiv(ARXIV_PAYLOAD)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["title"], "Scheduling Inference Across Untrusted Nodes"
        )
        self.assertEqual(items[0]["authors"], "Ada Lovelace, Alan Turing")
        self.assertEqual(items[0]["url"], "https://arxiv.org/abs/2511.00001v1")
        self.assertEqual(items[0]["date"], "2026-11-19")


class TestSlugify(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("Decentralized Inference!"), "decentralized-inference")
        self.assertEqual(slugify("   "), "digest")


if __name__ == "__main__":
    unittest.main()
