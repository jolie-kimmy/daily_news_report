#!/usr/bin/env python3
"""Generate a Markdown report for display-industry news."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - handled with a clear runtime message
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "sources.yaml"
REPORTS_DIR = ROOT / "reports"


@dataclass(frozen=True)
class Article:
    title: str
    link: str
    source: str
    published: dt.datetime | None
    summary: str
    topics: tuple[str, ...]
    score: int


def load_config() -> dict[str, Any]:
    if yaml is None:
        raise SystemExit(
            "Missing dependency: PyYAML. Install it with `pip install pyyaml`."
        )

    with SOURCES_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_feed(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-news-report/1.0 (+https://github.com/)",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+-\s+[^-]+$", "", title)
    title = re.sub(r"\W+", " ", title.lower())
    return title.strip()


def find_topics(text: str, topics: dict[str, list[str]]) -> tuple[str, ...]:
    matched: list[str] = []
    lowered = text.lower()
    for topic, terms in topics.items():
        if any(term.lower() in lowered for term in terms):
            matched.append(topic)
    return tuple(matched) if matched else ("General",)


def score_article(text: str, keywords: dict[str, list[str]]) -> int:
    lowered = text.lower()
    score = 0
    for section, terms in keywords.items():
        weight = 3 if section == "core" else 2 if section == "companies" else 1
        score += sum(weight for term in terms if term.lower() in lowered)
    return score


def parse_feed(
    feed_name: str,
    data: bytes,
    config: dict[str, Any],
    report_date: dt.date,
) -> list[Article]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        print(f"Skipping {feed_name}: invalid XML ({exc})", file=sys.stderr)
        return []

    items = root.findall("./channel/item")
    articles: list[Article] = []
    for item in items:
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        summary = clean_text(item.findtext("description"))
        published = parse_date(item.findtext("pubDate"))

        if not title or not link:
            continue
        if not is_in_report_window(published, report_date, config):
            continue

        combined = f"{title} {summary}"
        score = score_article(combined, config["keywords"])
        if score <= 0:
            continue

        articles.append(
            Article(
                title=title,
                link=link,
                source=feed_name,
                published=published,
                summary=summary,
                topics=find_topics(combined, config["topics"]),
                score=score,
            )
        )

    return articles


def is_in_report_window(
    published: dt.datetime | None,
    report_date: dt.date,
    config: dict[str, Any],
) -> bool:
    if published is None:
        return True

    lookback_days = int(config["report"].get("lookback_days", 7))
    start = dt.datetime.combine(
        report_date - dt.timedelta(days=lookback_days),
        dt.time.min,
        tzinfo=dt.timezone.utc,
    )
    end = dt.datetime.combine(
        report_date + dt.timedelta(days=1),
        dt.time.min,
        tzinfo=dt.timezone.utc,
    )
    return start <= published < end


def collect_articles(config: dict[str, Any], report_date: dt.date) -> list[Article]:
    by_title: dict[str, Article] = {}

    for feed in config["feeds"]:
        try:
            data = fetch_feed(feed["url"])
        except Exception as exc:  # noqa: BLE001 - keep scheduled reports resilient
            print(f"Skipping {feed['name']}: {exc}", file=sys.stderr)
            continue

        for article in parse_feed(feed["name"], data, config, report_date):
            key = normalize_title(article.title)
            current = by_title.get(key)
            if current is None or article.score > current.score:
                by_title[key] = article

    return sorted(
        by_title.values(),
        key=lambda item: (
            item.score,
            item.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        ),
        reverse=True,
    )


def short_summary(article: Article) -> str:
    source_suffix = f" ({article.source})"
    text = article.summary
    if not text:
        return f"Related display-industry item from{source_suffix}."
    text = re.sub(r"\s+", " ", text)
    if len(text) > 240:
        text = text[:237].rsplit(" ", 1)[0] + "..."
    return text


def render_report(report_date: dt.date, articles: list[Article], config: dict[str, Any]) -> str:
    max_items = int(config["report"].get("max_items", 25))
    selected = articles[:max_items]
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# {config['report']['title']} - {report_date.isoformat()}",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Executive Summary",
        "",
    ]

    if not selected:
        lines.extend(
            [
                "- No matching display-industry news items were found today.",
                "",
                "## Key News",
                "",
                "_No items._",
                "",
            ]
        )
        return "\n".join(lines)

    top_topics = topic_counts(selected)
    lines.extend(
        [
            f"- Collected {len(selected)} relevant items from configured feeds.",
            f"- Most active topics: {', '.join(top_topics[:5])}.",
            "- Review the source links before making business or investment decisions.",
            "",
            "## Key News",
            "",
        ]
    )

    for index, article in enumerate(selected, start=1):
        published = (
            article.published.strftime("%Y-%m-%d %H:%M UTC")
            if article.published
            else "Unknown"
        )
        lines.extend(
            [
                f"### {index}. {article.title}",
                "",
                f"- Source: {article.source}",
                f"- Published: {published}",
                f"- Topics: {', '.join(article.topics)}",
                f"- Relevance score: {article.score}",
                f"- Summary: {short_summary(article)}",
                f"- Link: {article.link}",
                "",
            ]
        )

    lines.extend(["## Topic View", ""])
    for topic, count in topic_counts_with_numbers(selected):
        lines.append(f"- {topic}: {count}")
    lines.append("")

    return "\n".join(lines)


def topic_counts(articles: list[Article]) -> list[str]:
    return [topic for topic, _ in topic_counts_with_numbers(articles)]


def topic_counts_with_numbers(articles: list[Article]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for article in articles:
        for topic in article.topics:
            counts[topic] = counts.get(topic, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="Report date in YYYY-MM-DD format. Defaults to today.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_date = dt.date.fromisoformat(args.date)
    config = load_config()
    articles = collect_articles(config, report_date)
    report = render_report(report_date, articles, config)

    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{report_date.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
