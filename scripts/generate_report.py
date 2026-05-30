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
    samsung_display_score: int


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


def score_samsung_display_relevance(
    text: str,
    feed_name: str,
    config: dict[str, Any],
) -> int:
    terms = config.get("samsung_display_relevance", {})
    lowered = text.lower()
    score = 0

    score += sum(35 for term in terms.get("exact", []) if term.lower() in lowered)
    score += sum(10 for term in terms.get("related", []) if term.lower() in lowered)

    if "samsung display" in feed_name.lower():
        score += 20
    if "samsung" in lowered and any(
        term in lowered for term in ("display", "oled", "panel", "microled", "qd-oled")
    ):
        score += 20

    return min(score, 100)


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
                samsung_display_score=score_samsung_display_relevance(
                    combined,
                    feed_name,
                    config,
                ),
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


def md_cell(value: object) -> str:
    text = str(value)
    text = text.replace("\n", " ").replace("|", r"\|")
    return text.strip()


def md_link_text(value: object) -> str:
    text = str(value).replace("\n", " ")
    return text.replace("[", r"\[").replace("]", r"\]").strip()


def article_text(article: Article) -> str:
    return " ".join(
        [
            article.title,
            article.summary,
            article.source,
            " ".join(article.topics),
        ]
    )


def keyword_hits(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lowered)


def classify_article(article: Article, config: dict[str, Any]) -> str:
    sections = config.get("report_sections", [])
    text = article_text(article)
    scores: dict[str, int] = {}

    for section in sections:
        section_id = section["id"]
        scores[section_id] = keyword_hits(text, section.get("keywords", []))

    preferred_order = [
        "competitor_moves",
        "customer_oem_signals",
        "market_trends",
        "materials_equipment_supply_chain",
    ]
    for section_id in preferred_order:
        if scores.get(section_id, 0) > 0:
            return section_id

    if article.samsung_display_score >= 70:
        return "samsung_display_focus"

    if scores.get("technology_watch", 0) > 0:
        return "technology_watch"

    return "technology_watch"


def group_articles_by_section(
    articles: list[Article],
    config: dict[str, Any],
) -> dict[str, list[Article]]:
    grouped = {section["id"]: [] for section in config.get("report_sections", [])}
    for article in articles:
        section_id = classify_article(article, config)
        grouped.setdefault(section_id, []).append(article)
    return grouped


def section_counts(grouped: dict[str, list[Article]], config: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for section in config.get("report_sections", []):
        count = len(grouped.get(section["id"], []))
        if count:
            labels.append(f"{section['title']}: {count}")
    return labels


def render_article_card(index: int, article: Article) -> list[str]:
    published = (
        article.published.strftime("%Y-%m-%d %H:%M UTC")
        if article.published
        else "Unknown"
    )
    return [
        f"#### {index}. [{md_link_text(article.title)}]({article.link})",
        "",
        "| Field | Detail |",
        "| --- | --- |",
        f"| Source | {md_cell(article.source)} |",
        f"| Published | {md_cell(published)} |",
        f"| Topics | {md_cell(', '.join(article.topics))} |",
        f"| Industry relevance | {article.score} |",
        f"| Samsung Display relevance | {article.samsung_display_score}/100 |",
        f"| Summary | {md_cell(short_summary(article))} |",
        f"| Original news | [Open article]({article.link}) |",
        "",
    ]


def strategic_implications(selected: list[Article], grouped: dict[str, list[Article]]) -> list[str]:
    samsung_count = len(grouped.get("samsung_display_focus", []))
    competitor_count = len(grouped.get("competitor_moves", []))
    technology_count = len(grouped.get("technology_watch", []))
    high_samsung = sum(1 for article in selected if article.samsung_display_score >= 70)

    implications = [
        f"- Samsung Display appears directly or strongly in {high_samsung} tracked article(s), making it the primary focus lens for this report.",
    ]
    if competitor_count:
        implications.append(
            f"- Competitor activity appears in {competitor_count} article(s), so pricing, product launches, and capacity moves should be watched closely."
        )
    if technology_count:
        implications.append(
            f"- Technology momentum appears in {technology_count} article(s), especially around OLED, QD-OLED, microLED, Mini LED, and refresh-rate differentiation."
        )
    if samsung_count == 0:
        implications.append(
            "- No direct Samsung Display section items were found, so indirect market and technology signals deserve closer review."
        )
    implications.append(
        "- Follow-up tracking should prioritize articles with high Samsung Display relevance and customer or competitor overlap."
    )
    return implications


def render_report(report_date: dt.date, articles: list[Article], config: dict[str, Any]) -> str:
    max_items = int(config["report"].get("max_items", 25))
    selected = articles[:max_items]
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top_topics = topic_counts(selected) if selected else []
    grouped = group_articles_by_section(selected, config) if selected else {}
    section_mix = section_counts(grouped, config) if selected else []

    lines = [
        f"# {config['report']['title']}",
        "",
        "> Display technology intelligence brief focused on market signals, product moves, and Samsung Display relevance.",
        "",
        f"**Report date:** `{report_date.isoformat()}` | **Generated:** `{generated_at}`",
        "",
        "## Signal Dashboard",
        "",
        "| Signal | Value |",
        "| --- | --- |",
        f"| Coverage window | Last {int(config['report'].get('lookback_days', 7))} days |",
        f"| Articles tracked | {len(selected)} |",
        f"| Main topics | {md_cell(', '.join(top_topics[:5]) if top_topics else 'None')} |",
        f"| Section mix | {md_cell('; '.join(section_mix) if section_mix else 'None')} |",
        f"| Focus lens | Samsung Display relevance |",
        "",
        "## Executive Summary",
        "",
    ]

    if not selected:
        lines.extend(
            [
                "- No matching display-industry news items were found today.",
                "",
                "## News by Theme",
                "",
                "_No items._",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            f"- {len(selected)} relevant display-industry signals were collected from configured news feeds.",
            f"- Topic momentum is concentrated around {', '.join(top_topics[:5])}.",
            f"- The report is organized by {len(section_mix)} active theme section(s): {'; '.join(section_mix)}.",
            "- Samsung Display relevance is scored from 0 to 100 using direct mentions, product overlap, and adjacent technology signals.",
            "",
            "## News by Theme",
            "",
        ]
    )

    for section in config.get("report_sections", []):
        section_articles = grouped.get(section["id"], [])
        if not section_articles:
            continue

        lines.extend(
            [
                f"### {section['title']}",
                "",
                f"> {section['description']}",
                "",
            ]
        )
        for index, article in enumerate(section_articles, start=1):
            lines.extend(render_article_card(index, article))

    lines.extend(["## Strategic Implications", ""])
    lines.extend(strategic_implications(selected, grouped))
    lines.append("")

    lines.extend(["## Topic Mix", ""])
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
