import pathlib
import tempfile
import unittest

import digest


class DigestTest(unittest.TestCase):
    def test_load_watchlist_with_builtin_parser(self):
        text = """
items:
  - code: "600519.SH"
    name: "贵州茅台"
    kind: "stock"
    note: "白酒龙头"
    keywords:
      - "贵州茅台"
      - "白酒"
"""
        records = digest._parse_watchlist_without_yaml(text)
        self.assertEqual(records[0]["code"], "600519.SH")
        self.assertEqual(records[0]["keywords"], ["贵州茅台", "白酒"])

    def test_build_digest_contains_disclaimer_and_no_trade_advice(self):
        item = digest.WatchItem(
            code="600519.SH",
            name="贵州茅台",
            kind="stock",
            note="白酒龙头",
            keywords=("贵州茅台",),
        )
        news = digest.NewsItem(title="贵州茅台发布业绩公告", source="测试源")
        content = digest.build_digest([item], {"600519.SH": [news]}, [])
        self.assertIn("不构成任何投资建议", content)
        self.assertNotIn("买入", content)
        self.assertNotIn("卖出", content)

    def test_build_digest_includes_quote_observation(self):
        item = digest.WatchItem(
            code="600519.SH",
            name="贵州茅台",
            kind="stock",
            note="白酒龙头",
            keywords=("贵州茅台",),
        )
        quote = digest.Quote(
            code="600519.SH",
            name="贵州茅台",
            price=1500.0,
            change_percent=3.2,
            change_amount=46.5,
            turnover=1234567890,
            turnover_rate=1.2,
            previous_close=1453.5,
            high=1510.0,
            low=1450.0,
        )
        index = digest.IndexQuote(code="000001", name="上证指数", change_percent=0.5)
        content = digest.build_digest([item], {"600519.SH": []}, [], quotes={"600519.SH": quote}, indices=[index])
        self.assertIn("最新/收盘价", content)
        self.assertIn("1500.00", content)
        self.assertIn("+3.20%", content)
        self.assertIn("今日重点留意", content)
        self.assertIn("上证指数", content)

    def test_operation_news_is_filtered(self):
        item = digest.WatchItem(
            code="601088.SH",
            name="中国神华",
            kind="stock",
            note="煤炭龙头",
            keywords=("中国神华",),
        )
        news = [
            digest.NewsItem(title="某投资人买入其他股票并清仓中国神华"),
            digest.NewsItem(title="中国神华发布分红公告"),
        ]
        content = digest.build_digest([item], {"601088.SH": news}, [], quotes={}, indices=[])
        self.assertNotIn("买入", content)
        self.assertNotIn("清仓", content)
        self.assertIn("分红公告", content)

    def test_missing_pushplus_token_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = pathlib.Path(tmp) / "watchlist.yaml"
            watchlist.write_text(
                """
items:
  - code: "000300.SH"
    name: "沪深300"
    kind: "fund"
""",
                encoding="utf-8",
            )
            args = digest.parse_args(["--watchlist", str(watchlist), "--send", "--timeout", "0.01"])
            with self.assertRaisesRegex(RuntimeError, "PUSHPLUS_TOKEN"):
                digest.run(args)

    def test_write_site_outputs_pwa_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            site_dir = pathlib.Path(tmp) / "site"
            digest.write_site(site_dir, "<p>今天正常观察。</p>")
            self.assertTrue((site_dir / "index.html").exists())
            self.assertTrue((site_dir / "manifest.webmanifest").exists())
            self.assertTrue((site_dir / "icon.svg").exists())
            html = (site_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("添加到手机桌面", html)
            self.assertIn("manifest.webmanifest", html)


if __name__ == "__main__":
    unittest.main()
