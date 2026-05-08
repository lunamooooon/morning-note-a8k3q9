#!/usr/bin/env python3
"""Daily web/WeChat digest for A-share stocks and domestic funds.

The script is intentionally dependency-light: it can preview a digest with only
the Python standard library, and it sends to pushplus when PUSHPLUS_TOKEN is set.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_WATCHLIST = ROOT / "watchlist.yaml"
DISCLAIMER = "仅为公开资讯整理，帮助了解情况，不构成任何投资建议。"
PUSHPLUS_URL = "https://www.pushplus.plus/send"
DEFAULT_SITE_DIR = ROOT / "site"


@dataclass(frozen=True)
class WatchItem:
    code: str
    name: str
    kind: str
    note: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str = ""
    source: str = ""
    published_at: str = ""
    summary: str = ""


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float | None = None
    change_percent: float | None = None
    change_amount: float | None = None
    turnover: float | None = None
    turnover_rate: float | None = None
    previous_close: float | None = None
    high: float | None = None
    low: float | None = None
    volume_ratio: float | None = None
    source: str = "AkShare"


@dataclass(frozen=True)
class IndexQuote:
    code: str
    name: str
    price: float | None = None
    change_percent: float | None = None
    change_amount: float | None = None


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_watchlist(path: Path) -> list[WatchItem]:
    text = path.read_text(encoding="utf-8")
    records = _load_yaml_records(text)
    items: list[WatchItem] = []
    for record in records:
        code = str(record.get("code", "")).strip()
        name = str(record.get("name", "")).strip()
        if not code or not name:
            raise ValueError(f"watchlist item must include code and name: {record}")
        kind = str(record.get("kind", "stock")).strip() or "stock"
        note = str(record.get("note", "")).strip()
        keywords = tuple(str(k).strip() for k in record.get("keywords", []) if str(k).strip())
        if name not in keywords:
            keywords = (name, *keywords)
        items.append(WatchItem(code=code, name=name, kind=kind, note=note, keywords=keywords))
    if not items:
        raise ValueError(f"watchlist is empty: {path}")
    return items


def _load_yaml_records(text: str) -> list[dict[str, Any]]:
    """Parse the simple watchlist YAML shape, using PyYAML when available."""
    try:
        import yaml  # type: ignore
    except Exception:
        yaml = None

    if yaml is not None:
        data = yaml.safe_load(text) or {}
        records = data.get("items", data if isinstance(data, list) else [])
        if not isinstance(records, list):
            raise ValueError("watchlist.yaml must contain an 'items' list")
        return [dict(record) for record in records]

    return _parse_watchlist_without_yaml(text)


def _parse_watchlist_without_yaml(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() == "items:":
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            payload = stripped[2:].strip()
            if ":" in payload:
                current = {}
                records.append(current)
                key, value = payload.split(":", 1)
                current[key.strip()] = _parse_scalar(value.strip())
                current_key = key.strip()
            elif current_key and current is not None:
                current.setdefault(current_key, []).append(_parse_scalar(payload))
            continue
        if current is None or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            current[key] = []
            current_key = key
        else:
            current[key] = _parse_scalar(value)
            current_key = key

    return records


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    return value.strip('"').strip("'")


def fetch_related_news(items: Iterable[WatchItem], timeout: float = 8.0) -> dict[str, list[NewsItem]]:
    results: dict[str, list[NewsItem]] = {}
    for item in items:
        collected: list[NewsItem] = []
        for keyword in item.keywords[:4]:
            collected.extend(fetch_eastmoney_search(keyword, timeout=timeout))
        collected.extend(fetch_tushare_news(item, timeout=timeout))
        results[item.code] = dedupe_news(collected)[:5]
    return results


def fetch_eastmoney_search(keyword: str, timeout: float = 8.0) -> list[NewsItem]:
    params = {
        "cb": "jQuery",
        "param": json.dumps(
            {
                "uid": "",
                "keyword": keyword,
                "type": ["cmsArticleWebOld"],
                "client": "web",
                "clientType": "web",
                "clientVersion": "curr",
                "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": 5}},
            },
            ensure_ascii=False,
        ),
    }
    url = "https://search-api-web.eastmoney.com/search/jsonp?" + urllib.parse.urlencode(params)
    try:
        body = http_get(url, timeout=timeout)
        match = re.search(r"jQuery\((.*)\)\s*$", body, re.S)
        payload = json.loads(match.group(1) if match else body)
        rows = payload.get("result", {}).get("cmsArticleWebOld", []) or []
    except Exception:
        return []

    news: list[NewsItem] = []
    for row in rows:
        title = clean_text(str(row.get("title") or row.get("showTitle") or ""))
        if not title:
            continue
        if should_exclude_news(title):
            continue
        news.append(
            NewsItem(
                title=title,
                url=str(row.get("url") or ""),
                source=str(row.get("source") or "东方财富"),
                published_at=str(row.get("date") or row.get("publishTime") or ""),
                summary=clean_text(str(row.get("content") or row.get("summary") or "")),
            )
        )
    return news


def fetch_tushare_news(item: WatchItem, timeout: float = 8.0) -> list[NewsItem]:
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        return []
    try:
        import tushare as ts  # type: ignore
    except Exception:
        return []

    yesterday = dt.date.today() - dt.timedelta(days=1)
    try:
        pro = ts.pro_api(token)
        frame = pro.news(src="eastmoney", start_date=yesterday.strftime("%Y-%m-%d 00:00:00"))
    except Exception:
        return []

    rows: list[NewsItem] = []
    for _, row in frame.head(80).iterrows():
        title = clean_text(str(row.get("title", "")))
        content = clean_text(str(row.get("content", "")))
        haystack = f"{title} {content}"
        if any(keyword and keyword in haystack for keyword in item.keywords):
            rows.append(
                NewsItem(
                    title=title,
                    source=str(row.get("src", "Tushare")),
                    published_at=str(row.get("datetime", "")),
                    summary=content[:120],
                )
            )
    return rows


def fetch_market_brief(timeout: float = 8.0) -> list[NewsItem]:
    topics = ("A股 市场", "基金 市场", "银行理财 政策")
    news: list[NewsItem] = []
    for topic in topics:
        news.extend(fetch_eastmoney_search(topic, timeout=timeout)[:2])
    return dedupe_news(news)[:5]


def fetch_quotes(items: Iterable[WatchItem]) -> tuple[dict[str, Quote], list[IndexQuote]]:
    """Fetch A-share quotes through AkShare when it is installed."""
    try:
        import akshare as ak  # type: ignore
    except Exception:
        return {}, []

    quotes: dict[str, Quote] = {}
    try:
        spot = ak.stock_zh_a_spot_em()
    except Exception:
        spot = None
    if spot is not None:
        for item in items:
            bare_code = normalize_a_share_code(item.code)
            try:
                row = spot.loc[spot["代码"].astype(str) == bare_code]
                if row.empty:
                    continue
                record = row.iloc[0]
            except Exception:
                continue
            quotes[item.code] = Quote(
                code=item.code,
                name=str(record.get("名称", item.name)),
                price=to_float(record.get("最新价")),
                change_percent=to_float(record.get("涨跌幅")),
                change_amount=to_float(record.get("涨跌额")),
                turnover=to_float(record.get("成交额")),
                turnover_rate=to_float(record.get("换手率")),
                previous_close=to_float(record.get("昨收")),
                high=to_float(record.get("最高")),
                low=to_float(record.get("最低")),
                volume_ratio=to_float(record.get("量比")),
            )

    indices: list[IndexQuote] = []
    try:
        index_spot = ak.stock_zh_index_spot_em()
    except Exception:
        index_spot = None
    if index_spot is not None:
        wanted = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指", "000300": "沪深300"}
        for code, fallback_name in wanted.items():
            try:
                row = index_spot.loc[index_spot["代码"].astype(str) == code]
                if row.empty:
                    continue
                record = row.iloc[0]
            except Exception:
                continue
            indices.append(
                IndexQuote(
                    code=code,
                    name=str(record.get("名称", fallback_name)),
                    price=to_float(record.get("最新价")),
                    change_percent=to_float(record.get("涨跌幅")),
                    change_amount=to_float(record.get("涨跌额")),
                )
            )
    return quotes, indices


def normalize_a_share_code(code: str) -> str:
    return code.split(".", 1)[0].strip()


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "nan", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def http_get(url: str, timeout: float = 8.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 DailyFinanceDigest/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def dedupe_news(news: Iterable[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    result: list[NewsItem] = []
    for item in news:
        key = clean_text(item.title)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def build_digest(
    items: list[WatchItem],
    related: dict[str, list[NewsItem]],
    market_news: list[NewsItem],
    quotes: dict[str, Quote] | None = None,
    indices: list[IndexQuote] | None = None,
) -> str:
    quotes = quotes or {}
    indices = indices or []
    today = dt.datetime.now().strftime("%Y年%m月%d日")
    related_count = sum(len([n for n in related.get(item.code, []) if "暂时获取失败" not in n.title]) for item in items)
    quote_count = sum(1 for item in items if item.code in quotes)
    alerts = collect_alerts(items, quotes, related)
    overview = build_overview(items, quotes, indices, related_count)

    lines = [
        '<section class="summary-panel" aria-labelledby="summary-title">',
        f'<p class="date-label">{html.escape(today)}</p>',
        '<h2 id="summary-title">昨天整体情况</h2>',
        f'<p class="summary-text">{html.escape(overview)}</p>',
        '<div class="metric-row" aria-label="早报统计">',
        f'<span class="metric"><strong>{len(items)}</strong> 个关注标的</span>',
        f'<span class="metric"><strong>{quote_count}</strong> 个有行情</span>',
        f'<span class="metric"><strong>{len(alerts)}</strong> 条提醒</span>',
        '</div>',
        '</section>',
        build_index_html(indices),
        build_alerts_html(alerts),
        '<section class="digest-section" aria-labelledby="watchlist-title">',
        '<h3 id="watchlist-title">持仓/关注清单观察</h3>',
    ]

    for item in items:
        quote = quotes.get(item.code)
        lines.append('<article class="watch-card">')
        lines.append('<div class="watch-card-header">')
        lines.append(f"<h4>{html.escape(item.name)}</h4>")
        lines.append(f'<span class="code-pill">{html.escape(item.code)}</span>')
        lines.append("</div>")
        lines.append(build_quote_html(quote, indices))
        if item.note:
            lines.append(f'<p class="watch-note">关注原因：{html.escape(item.note)}</p>')
        news = related.get(item.code, [])
        if news:
            lines.append('<div class="news-block"><p class="block-label">相关消息</p><ul class="news-list">')
        for news_item in prioritize_news(news)[:3]:
            lines.append(f"<li>{format_news_html(news_item)}<br>{explain_relevance(item, news_item)}</li>")
        if news:
            lines.append("</ul></div>")
        lines.append("</article>")

    lines.extend(["</section>", '<section class="digest-section" aria-labelledby="fund-title">', '<h3 id="fund-title">行业/理财相关动态</h3>', '<ul class="news-list compact">'])
    fund_news = [n for n in market_news if any(word in n.title for word in ("基金", "理财", "债", "指数"))][:3]
    if not fund_news:
        lines.append('<li class="quiet-state">暂时没有特别突出的基金/理财新闻，今天可以少看一点消息，多看长期变化。</li>')
    else:
        for news_item in fund_news:
            lines.append(f"<li>{format_news_html(news_item)}</li>")
    lines.append("</ul>")

    lines.extend(["</section>", '<section class="digest-section" aria-labelledby="market-title">', '<h3 id="market-title">大盘与政策简讯</h3>', '<ul class="news-list compact">'])
    for news_item in market_news[:4]:
        lines.append(f"<li>{format_news_html(news_item)}</li>")
    if not market_news:
        lines.append('<li class="quiet-state">市场简讯暂时获取失败，不影响关注清单本身的长期跟踪。</li>')
    lines.append("</ul>")

    lines.extend(["</section>", f'<p class="risk-note"><strong>温和提醒：</strong>{DISCLAIMER}</p>'])
    return "\n".join(lines)


def build_overview(items: list[WatchItem], quotes: dict[str, Quote], indices: list[IndexQuote], related_count: int) -> str:
    if not quotes:
        return f"今天先帮您看了 {len(items)} 个关注标的，但行情数据暂时没有取到；页面仍保留关注清单和相关新闻。"
    up = sum(1 for quote in quotes.values() if (quote.change_percent or 0) > 0)
    down = sum(1 for quote in quotes.values() if (quote.change_percent or 0) < 0)
    flat = len(quotes) - up - down
    market = indices[0] if indices else None
    market_text = ""
    if market and market.change_percent is not None:
        market_text = f" 大盘参考：{market.name}{format_percent_text(market.change_percent)}。"
    news_text = f" 同时整理到 {related_count} 条相关新闻。" if related_count else " 相关新闻较少，今天主要看行情和公告线索。"
    return f"最近交易日里，关注清单中 {up} 个上涨、{down} 个下跌、{flat} 个基本持平。{market_text}{news_text}"


def build_index_html(indices: list[IndexQuote]) -> str:
    if not indices:
        return '<section class="market-strip" aria-label="大盘参考"><p class="quiet-state">大盘指数暂时获取失败，先看个股自身变化。</p></section>'
    lines = ['<section class="market-strip" aria-label="大盘参考">']
    for item in indices[:4]:
        trend_class = trend_class_for(item.change_percent)
        lines.append(
            f'<div class="index-chip"><span>{html.escape(item.name)}</span>'
            f'<strong class="{trend_class}">{format_percent_text(item.change_percent)}</strong></div>'
        )
    lines.append("</section>")
    return "\n".join(lines)


def collect_alerts(items: list[WatchItem], quotes: dict[str, Quote], related: dict[str, list[NewsItem]]) -> list[str]:
    alerts: list[str] = []
    for item in items:
        quote = quotes.get(item.code)
        if quote:
            if quote.change_percent is not None and abs(quote.change_percent) >= 3:
                direction = "上涨" if quote.change_percent > 0 else "下跌"
                alerts.append(f"{item.name} 最近交易日{direction} {abs(quote.change_percent):.2f}%，波动偏大，适合看看是否有公告或行业消息配合。")
            if quote.turnover_rate is not None and quote.turnover_rate >= 5:
                alerts.append(f"{item.name} 换手率 {quote.turnover_rate:.2f}%，交易活跃度偏高，可以留意后续是否继续放量。")
            if quote.volume_ratio is not None and quote.volume_ratio >= 2:
                alerts.append(f"{item.name} 量比 {quote.volume_ratio:.2f}，成交节奏比平时更快。")
        important_news = [news for news in related.get(item.code, []) if is_important_news(news)]
        for news in important_news[:1]:
            alerts.append(f"{item.name} 有一条重点消息：{news.title}")
    return alerts[:6]


def build_alerts_html(alerts: list[str]) -> str:
    lines = ['<section class="digest-section" aria-labelledby="alerts-title">', '<h3 id="alerts-title">今日重点留意</h3>']
    if not alerts:
        lines.append('<p class="quiet-state">暂时没有发现特别明显的波动或重点公告，今天可以正常观察。</p>')
    else:
        lines.append('<ul class="alert-list">')
        for alert in alerts:
            lines.append(f"<li>{html.escape(alert)}</li>")
        lines.append("</ul>")
    lines.append("</section>")
    return "\n".join(lines)


def build_quote_html(quote: Quote | None, indices: list[IndexQuote]) -> str:
    if quote is None:
        return '<p class="quiet-state quote-fallback">行情暂时获取失败；如果安装 AkShare 并能访问数据源，这里会显示最近交易日收盘表现。</p>'
    trend_class = trend_class_for(quote.change_percent)
    comparison = compare_to_market(quote, indices)
    lines = [
        '<div class="quote-panel">',
        '<div class="quote-main">',
        '<span class="quote-label">最新/收盘价</span>',
        f'<strong>{format_price(quote.price)}</strong>',
        f'<span class="{trend_class}">{format_percent_text(quote.change_percent)} {format_amount_text(quote.change_amount)}</span>',
        '</div>',
        '<dl class="quote-grid">',
        f'<div><dt>成交额</dt><dd>{format_money(quote.turnover)}</dd></div>',
        f'<div><dt>换手率</dt><dd>{format_ratio_text(quote.turnover_rate)}</dd></div>',
        f'<div><dt>昨收</dt><dd>{format_price(quote.previous_close)}</dd></div>',
        f'<div><dt>最高/最低</dt><dd>{format_price(quote.high)} / {format_price(quote.low)}</dd></div>',
        '</dl>',
        f'<p class="quote-takeaway">{html.escape(build_quote_takeaway(quote, comparison))}</p>',
        '</div>',
    ]
    return "\n".join(lines)


def compare_to_market(quote: Quote, indices: list[IndexQuote]) -> str:
    if quote.change_percent is None or not indices or indices[0].change_percent is None:
        return ""
    gap = quote.change_percent - indices[0].change_percent
    if gap >= 1:
        return "明显强于大盘"
    if gap <= -1:
        return "明显弱于大盘"
    return "基本跟随大盘"


def build_quote_takeaway(quote: Quote, comparison: str) -> str:
    parts: list[str] = []
    if quote.change_percent is None:
        parts.append("行情变化暂时不完整。")
    elif quote.change_percent >= 3:
        parts.append("单日上涨幅度偏大。")
    elif quote.change_percent <= -3:
        parts.append("单日下跌幅度偏大。")
    elif abs(quote.change_percent) < 1:
        parts.append("整体波动不大。")
    else:
        parts.append("有一定波动。")
    if comparison:
        parts.append(comparison + "。")
    if quote.turnover_rate is not None and quote.turnover_rate >= 5:
        parts.append("交易活跃度偏高。")
    return "".join(parts)


def prioritize_news(news: list[NewsItem]) -> list[NewsItem]:
    filtered = [item for item in news if not should_exclude_news(f"{item.title} {item.summary}")]
    return sorted(filtered, key=lambda item: (0 if is_important_news(item) else 1, item.published_at), reverse=False)


def is_important_news(news_item: NewsItem) -> bool:
    text = f"{news_item.title} {news_item.summary}"
    keywords = ("公告", "业绩", "利润", "营收", "分红", "减持", "增持", "监管", "问询", "重大合同", "订单", "停牌", "复牌")
    return any(keyword in text for keyword in keywords)


def should_exclude_news(text: str) -> bool:
    operation_words = ("买入", "卖出", "加仓", "减仓", "清仓")
    return any(word in text for word in operation_words)


def trend_class_for(value: float | None) -> str:
    if value is None:
        return "trend-flat"
    if value > 0:
        return "trend-up"
    if value < 0:
        return "trend-down"
    return "trend-flat"


def format_price(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.2f}"


def format_percent_text(value: float | None) -> str:
    if value is None:
        return "--"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def format_ratio_text(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.2f}%"


def format_amount_text(value: float | None) -> str:
    if value is None:
        return ""
    sign = "+" if value > 0 else ""
    return f"({sign}{value:.2f})"


def format_money(value: float | None) -> str:
    if value is None:
        return "--"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.2f} 万"
    return f"{value:.0f}"


def build_web_page(body: str, generated_at: dt.datetime | None = None) -> str:
    generated_at = generated_at or dt.datetime.now()
    updated = generated_at.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#2f6f5e">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="理财早报">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <title>妈妈的理财资讯早报</title>
  <link rel="manifest" href="manifest.webmanifest">
  <link rel="icon" href="icon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="icon.svg">
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f1e8;
      --surface: #fffdf8;
      --surface-soft: #fbf7ed;
      --ink: #1f2c27;
      --muted: #66746d;
      --line: #ddd3c3;
      --accent: #236b5b;
      --accent-strong: #165143;
      --accent-soft: #e4f0e9;
      --gold: #b36b2d;
      --gold-soft: #f5ead8;
      --up: #b13d2e;
      --down: #1f7a55;
      --shadow: 0 18px 44px rgba(67, 54, 33, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    html {{
      background: var(--bg);
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
    }}
    body {{
      margin: 0;
      min-height: 100dvh;
      background:
        radial-gradient(circle at top left, rgba(35, 107, 91, 0.14), transparent 34rem),
        linear-gradient(180deg, #fbf8f1 0%, var(--bg) 42%, #efe8db 100%);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      font-size: 18px;
      line-height: 1.7;
    }}
    a:focus-visible {{
      outline: 3px solid rgba(35, 107, 91, 0.42);
      outline-offset: 3px;
      border-radius: 4px;
    }}
    .shell {{
      width: min(760px, 100%);
      margin: 0 auto;
      padding: max(18px, env(safe-area-inset-top)) 16px max(34px, env(safe-area-inset-bottom));
    }}
    header {{
      display: grid;
      grid-template-columns: 52px 1fr;
      gap: 14px;
      align-items: center;
      padding: 8px 2px 18px;
    }}
    .app-mark {{
      width: 52px;
      height: 52px;
      border-radius: 16px;
      background: var(--accent);
      box-shadow: 0 10px 24px rgba(35, 107, 91, 0.22);
      display: grid;
      place-items: center;
      flex: none;
    }}
    .app-mark svg {{
      width: 32px;
      height: 32px;
      display: block;
    }}
    .eyebrow {{
      margin: 0 0 6px;
      color: var(--accent);
      font-size: 15px;
      font-weight: 700;
    }}
    h1 {{
      margin: 0;
      font-size: 29px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .updated {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 15px;
    }}
    main {{
      padding: 0;
    }}
    .summary-panel {{
      background: linear-gradient(135deg, var(--accent-strong), var(--accent));
      color: #fffdf8;
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .date-label {{
      margin: 0 0 8px;
      color: rgba(255, 253, 248, 0.82);
      font-size: 15px;
      font-weight: 700;
    }}
    h2 {{
      margin: 0 0 10px;
      font-size: 26px;
      line-height: 1.3;
    }}
    .summary-text {{
      margin: 0;
      max-width: 38em;
      font-size: 19px;
    }}
    .metric-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    .metric {{
      min-height: 44px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 8px 12px;
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.14);
      border: 1px solid rgba(255, 253, 248, 0.24);
      color: rgba(255, 253, 248, 0.92);
      font-size: 15px;
    }}
    .metric strong {{
      font-size: 22px;
      line-height: 1;
      color: #fff7d8;
    }}
    .digest-section {{
      margin-top: 18px;
    }}
    .market-strip {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 0 0 18px;
    }}
    .index-chip {{
      min-height: 58px;
      background: rgba(255, 253, 248, 0.78);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 11px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 2px;
    }}
    .index-chip span {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .index-chip strong {{
      font-size: 17px;
      line-height: 1.2;
      font-variant-numeric: tabular-nums;
    }}
    h3 {{
      margin: 0 0 12px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
      color: var(--gold);
      font-size: 21px;
      line-height: 1.3;
    }}
    .digest-section:first-of-type h3 {{
      border-top: 0;
      padding-top: 0;
    }}
    .watch-card {{
      background: var(--surface-soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin: 12px 0;
    }}
    .watch-card-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }}
    h4 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.35;
    }}
    .code-pill {{
      flex: none;
      min-height: 32px;
      display: inline-flex;
      align-items: center;
      border-radius: 8px;
      padding: 4px 9px;
      background: #fff8ea;
      border: 1px solid #ead7b8;
      color: #6f481f;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    p {{ margin: 8px 0; }}
    .watch-note {{
      color: var(--muted);
      font-size: 16px;
      margin: 8px 0 0;
    }}
    .quote-panel {{
      margin: 12px 0;
      padding: 12px;
      border: 1px solid #ead7b8;
      border-radius: 8px;
      background: #fffaf0;
    }}
    .quote-main {{
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: end;
      gap: 4px 10px;
      margin-bottom: 10px;
    }}
    .quote-label {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      grid-column: 1 / -1;
    }}
    .quote-main strong {{
      font-size: 30px;
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }}
    .quote-main span:last-child {{
      font-size: 18px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}
    .quote-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 0;
    }}
    .quote-grid div {{
      min-height: 54px;
      border-radius: 8px;
      background: var(--surface);
      border: 1px solid #efe2cb;
      padding: 8px 9px;
    }}
    .quote-grid dt {{
      color: var(--muted);
      font-size: 13px;
    }}
    .quote-grid dd {{
      margin: 2px 0 0;
      color: var(--ink);
      font-size: 16px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}
    .quote-takeaway {{
      margin: 10px 0 0;
      color: #4d463c;
      font-size: 15px;
    }}
    .quote-fallback {{
      margin-top: 12px;
    }}
    ul {{
      margin: 8px 0 0;
      padding-left: 1.2em;
    }}
    .news-list {{
      padding-left: 0;
      list-style: none;
    }}
    .news-list li {{
      position: relative;
      margin: 10px 0;
      padding-left: 18px;
    }}
    .news-list li::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 0.78em;
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--accent);
    }}
    .news-list.compact {{
      background: var(--surface-soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 14px;
    }}
    a {{
      color: var(--accent);
      text-decoration-thickness: 1px;
      text-underline-offset: 3px;
    }}
    a:hover {{ color: var(--accent-strong); }}
    strong {{ font-weight: 700; }}
    span {{
      display: inline-block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 16px;
    }}
    .trend-up {{
      color: var(--up) !important;
    }}
    .trend-down {{
      color: var(--down) !important;
    }}
    .trend-flat {{
      color: var(--muted) !important;
    }}
    .alert-list {{
      margin: 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 10px;
    }}
    .alert-list li {{
      border-radius: 8px;
      border: 1px solid #e7d0a9;
      background: var(--gold-soft);
      padding: 11px 12px;
      color: #503a20;
    }}
    .news-block {{
      margin-top: 12px;
    }}
    .block-label {{
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 15px;
      font-weight: 700;
    }}
    .quiet-state {{
      color: #43544d;
      background: var(--accent-soft);
      border: 1px solid #caddd3;
      border-radius: 8px;
      padding: 10px 12px;
    }}
    .risk-note {{
      margin: 20px 0 0;
      padding: 12px 14px;
      border-radius: 8px;
      background: var(--gold-soft);
      border: 1px solid #e7d0a9;
      color: #503a20;
      font-size: 16px;
    }}
    .home-tip {{
      margin-top: 16px;
      background: rgba(255, 253, 248, 0.74);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      color: #40534b;
      font-size: 16px;
    }}
    @media (prefers-reduced-motion: no-preference) {{
      a {{
        transition: color 180ms ease-out;
      }}
    }}
    @media (max-width: 520px) {{
      body {{ font-size: 17px; }}
      .shell {{ padding: max(12px, env(safe-area-inset-top)) 10px max(24px, env(safe-area-inset-bottom)); }}
      header {{ grid-template-columns: 48px 1fr; gap: 12px; }}
      .app-mark {{ width: 48px; height: 48px; border-radius: 14px; }}
      main {{ padding: 0; }}
      h1 {{ font-size: 25px; }}
      h2 {{ font-size: 23px; }}
      h3 {{ font-size: 20px; }}
      .summary-panel {{ padding: 16px; }}
      .summary-text {{ font-size: 18px; }}
      .watch-card {{ padding: 13px; }}
      .watch-card-header {{ display: block; }}
      .code-pill {{ margin-top: 8px; }}
      .market-strip {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
      .quote-main strong {{ font-size: 28px; }}
      .quote-grid {{ gap: 7px; }}
      .quote-grid dd {{ font-size: 15px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="app-mark" aria-hidden="true">
        <svg viewBox="0 0 48 48" role="img">
          <path d="M11 31c6.8-9 12.8-13.2 18-12.8 4.7.3 8.4 3.8 11 10.2" fill="none" stroke="#fffdf8" stroke-width="3.6" stroke-linecap="round"/>
          <path d="M12 36h24" stroke="#f2cc82" stroke-width="3.8" stroke-linecap="round"/>
          <path d="M30 10h7v7" fill="none" stroke="#f2cc82" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M24 23l13-13" stroke="#f2cc82" stroke-width="3.2" stroke-linecap="round"/>
        </svg>
      </div>
      <div>
        <p class="eyebrow">每日更新</p>
        <h1>妈妈的理财资讯早报</h1>
        <p class="updated">更新时间：{html.escape(updated)}</p>
      </div>
    </header>
    <main>
      {body}
    </main>
    <p class="home-tip">添加到手机桌面后，点“理财早报”图标就能看到最新内容。</p>
  </div>
</body>
</html>
"""


def write_site(site_dir: Path, body: str) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "index.html").write_text(build_web_page(body), encoding="utf-8")
    (site_dir / "manifest.webmanifest").write_text(json.dumps(site_manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    (site_dir / "icon.svg").write_text(site_icon_svg(), encoding="utf-8")


def site_manifest() -> dict[str, Any]:
    return {
        "name": "妈妈的理财资讯早报",
        "short_name": "理财早报",
        "start_url": "./index.html",
        "scope": "./",
        "display": "standalone",
        "background_color": "#f7f3eb",
        "theme_color": "#2f6f5e",
        "icons": [
            {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
        ],
    }


def site_icon_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#2f6f5e"/>
  <circle cx="142" cy="164" r="38" fill="#f7f3eb"/>
  <path d="M116 332c58-86 113-130 165-132 49-2 86 31 116 92" fill="none" stroke="#f7f3eb" stroke-width="34" stroke-linecap="round"/>
  <path d="M132 382h248" stroke="#e8c27a" stroke-width="34" stroke-linecap="round"/>
  <path d="M326 143h62v62" fill="none" stroke="#e8c27a" stroke-width="28" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M268 229l118-118" stroke="#e8c27a" stroke-width="28" stroke-linecap="round"/>
</svg>
"""


def format_news_html(news_item: NewsItem) -> str:
    title = html.escape(news_item.title)
    if news_item.url:
        title = f'<a href="{html.escape(news_item.url)}">{title}</a>'
    meta_parts = [part for part in (news_item.source, news_item.published_at) if part]
    meta = f"（{html.escape('｜'.join(meta_parts))}）" if meta_parts else ""
    summary = f"：{html.escape(news_item.summary[:120])}" if news_item.summary else ""
    return f"{title}{meta}{summary}"


def explain_relevance(item: WatchItem, news_item: NewsItem) -> str:
    title = news_item.title
    if any(word in title for word in ("公告", "业绩", "利润", "营收", "分红")):
        text = "这类消息通常和公司经营或分红安排有关，适合看看是否符合原来的持有理由。"
    elif any(word in title for word in ("政策", "监管", "降息", "利率", "央行")):
        text = "这类消息偏宏观，会影响市场情绪，不需要因为单条消息着急操作。"
    elif item.kind == "fund" or any(word in title for word in ("基金", "指数", "债券")):
        text = "这类消息更适合放在基金风格和长期表现里一起看，不看一天涨跌下结论。"
    else:
        text = "这条消息和关注标的有关，可以先了解背景，再结合后续公告和市场反应观察。"
    return f"<span>{html.escape(text)}</span>"


def send_pushplus(title: str, content: str, token: str) -> None:
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
        "channel": os.getenv("PUSHPLUS_CHANNEL", "wechat"),
    }
    topic = os.getenv("PUSHPLUS_TOPIC", "").strip()
    if topic:
        payload["topic"] = topic
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        PUSHPLUS_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"pushplus send failed: {exc}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pushplus returned non-JSON response: {body[:200]}") from exc
    if str(result.get("code")) not in {"200", "0"}:
        raise RuntimeError(f"pushplus send failed: {result}")


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if args.send and not token:
        raise RuntimeError("PUSHPLUS_TOKEN is missing. Copy .env.example to .env and fill it in.")
    items = load_watchlist(Path(args.watchlist))
    quotes, indices = fetch_quotes(items)
    related = fetch_related_news(items, timeout=args.timeout)
    market_news = fetch_market_brief(timeout=args.timeout)
    content = build_digest(items, related, market_news, quotes=quotes, indices=indices)
    title = f"妈妈的理财资讯早报 {dt.datetime.now().strftime('%m-%d')}"

    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
    if args.site:
        write_site(Path(args.site), content)
        print(f"site written to {Path(args.site).resolve()}")
    if args.preview or not args.send:
        print(textwrap.dedent(content).strip())
    if args.send:
        send_pushplus(title, content, token)
        print("pushplus message sent")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and optionally send a daily finance digest.")
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST), help="Path to watchlist.yaml")
    parser.add_argument("--output", help="Write generated HTML to a file")
    parser.add_argument("--site", nargs="?", const=str(DEFAULT_SITE_DIR), help="Write a mobile-friendly static site")
    parser.add_argument("--send", action="store_true", help="Send digest via pushplus")
    parser.add_argument("--preview", action="store_true", help="Print digest to stdout")
    parser.add_argument("--timeout", type=float, default=8.0, help="HTTP timeout in seconds")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args(sys.argv[1:])))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
