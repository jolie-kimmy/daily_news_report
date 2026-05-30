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
DOCS_DIR = ROOT / "docs"


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


def html_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_score(score: int) -> str:
    return f"""
    <div class="score" aria-label="Samsung Display relevance {score} out of 100">
      <span>{score}</span>
      <small>/100</small>
    </div>
    """


def render_html_article(article: Article) -> str:
    published = (
        article.published.strftime("%Y-%m-%d %H:%M UTC")
        if article.published
        else "Unknown"
    )
    topics = "".join(f"<span>{html_escape(topic)}</span>" for topic in article.topics)
    return f"""
      <article class="news-card">
        <div class="news-topline">
          <div class="topic-pills">{topics}</div>
          {render_score(article.samsung_display_score)}
        </div>
        <h3><a href="{html_escape(article.link)}" target="_blank" rel="noreferrer">{html_escape(article.title)}</a></h3>
        <p>{html_escape(short_summary(article))}</p>
        <dl>
          <div><dt>Source</dt><dd>{html_escape(article.source)}</dd></div>
          <div><dt>Published</dt><dd>{html_escape(published)}</dd></div>
          <div><dt>Industry relevance</dt><dd>{article.score}</dd></div>
        </dl>
      </article>
    """


def render_html_report(report_date: dt.date, articles: list[Article], config: dict[str, Any]) -> str:
    max_items = int(config["report"].get("max_items", 25))
    selected = articles[:max_items]
    grouped = group_articles_by_section(selected, config) if selected else {}
    section_mix = section_counts(grouped, config) if selected else []
    top_topics = topic_counts(selected) if selected else []
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    section_blocks: list[str] = []
    for section in config.get("report_sections", []):
        section_articles = grouped.get(section["id"], [])
        if not section_articles:
            continue
        cards = "\n".join(render_html_article(article) for article in section_articles)
        section_blocks.append(
            f"""
            <section class="theme-section">
              <div class="section-heading">
                <p>{html_escape(section['description'])}</p>
                <h2>{html_escape(section['title'])}</h2>
              </div>
              <div class="news-grid">{cards}</div>
            </section>
            """
        )

    implications = "\n".join(
        f"<li>{html_escape(item.removeprefix('- '))}</li>"
        for item in strategic_implications(selected, grouped)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(config['report']['title'])}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #070a10;
      --panel: rgba(15, 22, 34, 0.78);
      --panel-strong: rgba(22, 31, 48, 0.92);
      --line: rgba(134, 194, 255, 0.2);
      --text: #eef6ff;
      --muted: #9fb1c7;
      --cyan: #6ee7ff;
      --blue: #7aa8ff;
      --magenta: #ff7ad9;
      --green: #9cffc7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: radial-gradient(circle at 25% 0%, rgba(73, 116, 255, 0.22), transparent 34%),
        radial-gradient(circle at 82% 12%, rgba(255, 122, 217, 0.12), transparent 30%),
        var(--bg);
      color: var(--text);
    }}
    a {{ color: inherit; }}
    .hero {{
      min-height: 560px;
      display: grid;
      align-items: end;
      background-image: linear-gradient(90deg, rgba(7, 10, 16, 0.96), rgba(7, 10, 16, 0.72) 42%, rgba(7, 10, 16, 0.15)),
        url("assets/display-hero.png");
      background-size: cover;
      background-position: center;
      border-bottom: 1px solid var(--line);
    }}
    .hero-inner, main {{ width: min(1180px, calc(100% - 40px)); margin: 0 auto; }}
    .hero-inner {{ padding: 72px 0 54px; }}
    .eyebrow {{
      color: var(--cyan);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    h1 {{ margin: 12px 0 18px; font-size: clamp(42px, 8vw, 88px); line-height: 0.95; letter-spacing: 0; max-width: 760px; }}
    .hero p {{ max-width: 660px; color: #c7d8ed; font-size: 18px; line-height: 1.65; }}
    .dashboard {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 34px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      backdrop-filter: blur(14px);
    }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 22px; }}
    main {{ padding: 42px 0 72px; }}
    .summary, .theme-section {{
      border-top: 1px solid var(--line);
      padding-top: 28px;
      margin-top: 30px;
    }}
    .summary ul, .implications ul {{ margin: 0; padding-left: 20px; color: #c9d8ea; line-height: 1.7; }}
    .section-heading {{ margin-bottom: 18px; }}
    .section-heading p {{ color: var(--cyan); margin: 0 0 8px; font-size: 14px; }}
    h2 {{ margin: 0; font-size: 28px; }}
    .news-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .news-card {{
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.25);
    }}
    .news-topline {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }}
    .topic-pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .topic-pills span {{
      border: 1px solid rgba(110, 231, 255, 0.28);
      color: #bfefff;
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 12px;
    }}
    .score {{
      min-width: 68px;
      text-align: right;
      color: var(--green);
      font-weight: 800;
    }}
    .score span {{ font-size: 24px; }}
    .score small {{ color: var(--muted); }}
    .news-card h3 {{ margin: 16px 0 10px; font-size: 19px; line-height: 1.35; }}
    .news-card h3 a {{ text-decoration: none; }}
    .news-card h3 a:hover {{ color: var(--cyan); }}
    .news-card p {{ color: #c8d5e6; line-height: 1.6; }}
    dl {{ display: grid; gap: 8px; margin: 18px 0 0; }}
    dl div {{ display: flex; justify-content: space-between; gap: 16px; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 8px; }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; text-align: right; color: #dbe9f8; }}
    footer {{ color: var(--muted); border-top: 1px solid var(--line); padding: 24px 0; }}
    @media (max-width: 820px) {{
      .hero {{ min-height: 640px; }}
      .dashboard, .news-grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 46px; }}
    }}
  </style>
</head>
<body>
  <header class="hero">
    <div class="hero-inner">
      <div class="eyebrow">Display Intelligence</div>
      <h1>{html_escape(config['report']['title'])}</h1>
      <p>Market signals, customer moves, competitor activity, and technology developments organized through a Samsung Display relevance lens.</p>
      <div class="dashboard">
        <div class="metric"><span>Report date</span><strong>{report_date.isoformat()}</strong></div>
        <div class="metric"><span>Articles tracked</span><strong>{len(selected)}</strong></div>
        <div class="metric"><span>Main topics</span><strong>{html_escape(', '.join(top_topics[:2]) if top_topics else 'None')}</strong></div>
        <div class="metric"><span>Generated</span><strong>{html_escape(generated_at)}</strong></div>
      </div>
    </div>
  </header>
  <main>
    <section class="summary">
      <div class="section-heading">
        <p>Signal Dashboard</p>
        <h2>Executive Summary</h2>
      </div>
      <ul>
        <li>{len(selected)} relevant display-industry signals were collected from configured feeds.</li>
        <li>Active sections: {html_escape('; '.join(section_mix) if section_mix else 'None')}.</li>
        <li>Samsung Display relevance is scored from 0 to 100 using direct mentions, product overlap, and adjacent technology signals.</li>
      </ul>
    </section>
    {''.join(section_blocks)}
    <section class="theme-section implications">
      <div class="section-heading">
        <p>Decision Lens</p>
        <h2>Strategic Implications</h2>
      </div>
      <ul>{implications}</ul>
    </section>
  </main>
  <footer>
    <main>Generated from RSS feeds. Open each source link before making business or investment decisions.</main>
  </footer>
</body>
</html>
"""


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
    html_report = render_html_report(report_date, articles, config)

    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{report_date.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html_report, encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
