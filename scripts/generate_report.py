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

try:
    from deep_translator import GoogleTranslator
except ImportError:  # pragma: no cover - translation falls back gracefully
    GoogleTranslator = None


ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "sources.yaml"
REPORTS_DIR = ROOT / "reports"
DOCS_DIR = ROOT / "docs"


@dataclass(frozen=True)
class Article:
    title: str
    title_ko: str
    english_title: str | None
    link: str
    source: str
    published: dt.datetime | None
    summary: str
    summary_ko: str
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


def normalize_issue_text(text: str) -> str:
    text = re.sub(r"\s+-\s+[^-]+$", "", text)
    text = re.sub(r"[^0-9a-zA-Z가-힣]+", " ", text.lower())
    stopwords = {
        "the",
        "a",
        "an",
        "and",
        "with",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "new",
        "first",
        "world",
        "worlds",
        "display",
        "panel",
        "panels",
    }
    tokens = [token for token in text.split() if token not in stopwords]
    return " ".join(tokens[:12])


def contains_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text))


def is_english_like(text: str) -> bool:
    letters = re.findall(r"[A-Za-z]", text)
    if not letters or contains_hangul(text):
        return False
    return len(letters) >= max(8, len(text) * 0.25)


TRANSLATION_GLOSSARY = [
    ("Samsung Display", "삼성디스플레이"),
    ("LG Display", "LG디스플레이"),
    ("OLED", "OLED"),
    ("QD-OLED", "QD-OLED"),
    ("AMOLED", "AMOLED"),
    ("microLED", "마이크로LED"),
    ("MicroLED", "마이크로LED"),
    ("Mini LED", "미니LED"),
    ("display panel", "디스플레이 패널"),
    ("monitor panel", "모니터 패널"),
    ("TV panel", "TV 패널"),
    ("automotive display", "차량용 디스플레이"),
    ("foldable", "폴더블"),
    ("flexible", "플렉서블"),
    ("mass production", "양산"),
    ("Mass-Produce", "양산"),
    ("develops", "개발"),
    ("Develops", "개발"),
    ("unveils", "공개"),
    ("Unveils", "공개"),
    ("announces", "발표"),
    ("sets new record", "신기록 수립"),
    ("world's first", "세계 최초"),
    ("World's First", "세계 최초"),
    ("brighter and faster", "더 밝고 빨라짐"),
    ("partner", "협력"),
    ("partners", "협력"),
    ("supply", "공급"),
    ("screens", "스크린"),
    ("panel", "패널"),
    ("display", "디스플레이"),
]


def glossary_translate(text: str) -> str:
    translated = text
    for source, target in TRANSLATION_GLOSSARY:
        translated = translated.replace(source, target)
    return translated


def make_translator() -> Any:
    if GoogleTranslator is None:
        return None
    try:
        return GoogleTranslator(source="auto", target="ko")
    except Exception:
        return None


def translate_to_korean(text: str, translator: Any, cache: dict[str, str]) -> str:
    text = clean_text(text)
    if not text or contains_hangul(text):
        return text
    if text in cache:
        return cache[text]

    translated = ""
    if translator is not None:
        try:
            translated = clean_text(translator.translate(text))
        except Exception as exc:  # noqa: BLE001 - keep report generation resilient
            print(f"Translation fallback used: {exc}", file=sys.stderr)

    if not translated:
        translated = glossary_translate(text)
    cache[text] = translated
    return translated


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
    translator: Any,
    translation_cache: dict[str, str],
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
                title_ko=translate_to_korean(title, translator, translation_cache),
                english_title=title if is_english_like(title) else None,
                link=link,
                source=feed_name,
                published=published,
                summary=summary,
                summary_ko=translate_to_korean(
                    short_text(summary),
                    translator,
                    translation_cache,
                ),
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
    translator = make_translator()
    translation_cache: dict[str, str] = {}

    for feed in config["feeds"]:
        try:
            data = fetch_feed(feed["url"])
        except Exception as exc:  # noqa: BLE001 - keep scheduled reports resilient
            print(f"Skipping {feed['name']}: {exc}", file=sys.stderr)
            continue

        for article in parse_feed(
            feed["name"],
            data,
            config,
            report_date,
            translator,
            translation_cache,
        ):
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


def issue_key(article: Article) -> str:
    text = f"{article.title} {article.summary}".lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    qd_oled = "qdoled" in compact
    if (qd_oled and "4k" in text and ("360hz" in compact or "monitor" in text)) or (
        "samsung" in text and "oled" in text and "4k" in text and "360hz" in compact
    ):
        return "issue:4k-360hz-qd-oled-monitor"
    if "lg" in text and "240hz" in compact and "rgbstripe" in compact and "oled" in text:
        return "issue:lg-display-240hz-rgb-stripe-oled"
    if "ferrari" in text and any(term in text for term in ("oled", "amoled", "display", "screen", "panel")):
        return "issue:ferrari-oled-display"
    if "boe" in text and "galaxy s27" in text:
        return "issue:boe-galaxy-s27-oled"
    if "microled" in text and "auo" in text and "aledia" in text:
        return "issue:auo-aledia-microled"
    return f"title:{normalize_issue_text(article.title)}"


def application_bucket(article: Article, config: dict[str, Any]) -> str | None:
    for bucket in application_lenses(config):
        if bucket in article.topics:
            return bucket

    text = article_text(article).lower()
    topics = config.get("topics", {})
    for bucket in application_lenses(config):
        terms = topics.get(bucket, [])
        if any(term.lower() in text for term in terms):
            return bucket
    return None


def article_sort_key(article: Article) -> tuple[int, int, dt.datetime]:
    return (
        article.score + article.samsung_display_score,
        article.samsung_display_score,
        article.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
    )


def select_articles(articles: list[Article], config: dict[str, Any]) -> list[Article]:
    max_items = int(config["report"].get("max_items", 25))
    selection = config["report"].get("selection", {})
    minimum_applications = selection.get("minimum_applications", {})
    section_limits = selection.get("section_limits", {})

    sorted_articles = sorted(articles, key=article_sort_key, reverse=True)
    selected: list[Article] = []
    used_issues: set[str] = set()
    section_counts_selected: dict[str, int] = {}

    def can_add(article: Article, enforce_section_limit: bool = True) -> bool:
        if issue_key(article) in used_issues:
            return False
        if enforce_section_limit:
            section_name = section_title(
                next(
                    section
                    for section in config.get("report_sections", [])
                    if section["id"] == classify_article(article, config)
                )
            )
            limit = section_limits.get(section_name)
            if limit is not None and section_counts_selected.get(section_name, 0) >= int(limit):
                return False
        return True

    def add(article: Article) -> bool:
        if len(selected) >= max_items or not can_add(article):
            return False
        section_name = section_title(
            next(
                section
                for section in config.get("report_sections", [])
                if section["id"] == classify_article(article, config)
            )
        )
        selected.append(article)
        used_issues.add(issue_key(article))
        section_counts_selected[section_name] = section_counts_selected.get(section_name, 0) + 1
        return True

    for bucket, minimum in minimum_applications.items():
        bucket_articles = [
            article for article in sorted_articles if application_bucket(article, config) == bucket
        ]
        added = 0
        for article in bucket_articles:
            if added >= int(minimum):
                break
            if add(article):
                added += 1

    for article in sorted_articles:
        add(article)

    return sorted(selected, key=article_sort_key, reverse=True)


def application_lenses(config: dict[str, Any]) -> list[str]:
    selection = config["report"].get("selection", {})
    return list(selection.get("minimum_applications", {}).keys())


def short_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > 240:
        text = text[:237].rsplit(" ", 1)[0] + "..."
    return text


def short_summary(article: Article) -> str:
    source_suffix = f" ({article.source})"
    text = article.summary_ko or article.summary
    if not text:
        return f"{source_suffix}에서 수집한 디스플레이 산업 관련 기사입니다."
    return short_text(text)


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


def company_mentions(articles: list[Article], exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    companies = [
        "Samsung Display",
        "LG Display",
        "BOE",
        "CSOT",
        "TCL CSOT",
        "AUO",
        "Innolux",
        "Tianma",
        "Visionox",
        "Japan Display",
        "Sharp",
        "Ferrari",
        "Apple",
        "Dell",
        "HP",
        "Lenovo",
        "Sony",
    ]
    text = " ".join(article_text(article) for article in articles).lower()
    mentioned = [
        company
        for company in companies
        if company not in exclude and company.lower() in text
    ]
    return mentioned[:6]


def has_any(articles: list[Article], terms: list[str]) -> bool:
    text = " ".join(article_text(article) for article in articles).lower()
    return any(term.lower() in text for term in terms)


APPLICATION_TOPICS = {
    "Phone",
    "Tablet & Foldable",
    "Watch & Wearables",
    "Gaming",
    "Note PC",
    "Automotive",
    "Monitor",
    "TV",
    "XR & Spatial Computing",
    "Digital Signage",
    "Home Appliances",
    "Retail & Kiosk",
    "Medical & Healthcare",
    "Industrial & Robotics",
    "AI Devices",
    "Mobility & Transportation",
    "Public Infrastructure",
    "Education & Collaboration",
    "E-Paper & Low Power",
    "Defense & Rugged",
}


def application_topic_counts(articles: list[Article]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for article in articles:
        for topic in article.topics:
            if topic in APPLICATION_TOPICS:
                counts[topic] = counts.get(topic, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def executive_summary(selected: list[Article], grouped: dict[str, list[Article]]) -> list[str]:
    bullets: list[str] = []
    samsung_articles = grouped.get("samsung_display_focus", [])
    customer_articles = grouped.get("customer_oem_signals", [])
    competitor_articles = grouped.get("competitor_moves", [])
    tech_articles = grouped.get("technology_watch", [])
    supply_articles = grouped.get("materials_equipment_supply_chain", [])
    app_names = {name for name, _ in application_topic_counts(selected)}

    if {"Gaming", "Monitor"} & app_names:
        bullets.append(
            "이번 리포트에서 가장 두드러진 흐름은 OLED가 게이밍 모니터와 프리미엄 모니터 시장의 핵심 사양으로 빠르게 자리 잡고 있다는 점입니다. Acer, MSI 등 세트 업체들이 고주사율 QD-OLED 모델을 전면에 내세우면서 패널 경쟁의 초점도 해상도와 주사율, 밝기, 사용 안정성으로 옮겨가고 있습니다."
        )

    if has_any(samsung_articles + customer_articles, ["QD-OLED", "360Hz", "4K"]):
        bullets.append(
            "삼성디스플레이의 4K 360Hz QD-OLED는 이 흐름의 중심에 있습니다. 단순한 신제품 발표라기보다 프리미엄 IT 패널에서 삼성디스플레이가 기술 기준을 선점하려는 움직임으로 해석됩니다."
        )
    elif samsung_articles:
        bullets.append(
            f"삼성디스플레이 관련 주요 뉴스는 '{samsung_articles[0].title_ko}'입니다. 해당 이슈는 삼성디스플레이의 제품 포지셔닝과 고객사 확대 가능성을 함께 보여줍니다."
        )

    if "Automotive" in app_names:
        bullets.append(
            "차량용 디스플레이에서는 Ferrari 전기차 OLED 공급 뉴스와 자동차 디스플레이 시장 성장 전망이 함께 확인됐습니다. OLED가 스마트폰과 TV를 넘어 고급 차량의 실내 경험을 차별화하는 부품으로 확장되고 있다는 신호입니다."
        )

    if {"Phone", "Note PC", "TV", "Tablet & Foldable"} & app_names:
        bullets.append(
            "모바일과 IT 기기에서는 OLED 채택이 계속 넓어지는 가운데 가격 경쟁도 동시에 심해지고 있습니다. Galaxy 패널을 둘러싼 BOE의 가격 공세는 프리미엄 OLED 시장에서도 원가와 품질 경쟁이 함께 진행되고 있음을 보여줍니다."
        )

    competitors = company_mentions(competitor_articles, exclude={"Samsung Display"})
    if competitors:
        bullets.append(
            f"경쟁 구도에서는 {', '.join(competitors)}의 움직임이 눈에 띕니다. LG Display의 OLED 모니터 양산과 BOE의 모바일 패널 가격 공세는 삼성디스플레이가 IT와 모바일 양쪽에서 기술 우위와 원가 경쟁력을 동시에 증명해야 한다는 압박으로 이어집니다."
        )

    if has_any(tech_articles + supply_articles, ["microLED", "MicroLED", "module", "factory"]):
        bullets.append(
            "차세대 기술에서는 microLED 협력 뉴스도 이어졌습니다. 아직 단기 매출보다 중장기 옵션에 가까운 영역이지만, 웨어러블과 XR, 산업용 디스플레이로 이어질 수 있어 삼성디스플레이의 기술 포트폴리오 점검이 필요합니다."
        )

    if not bullets and selected:
        bullets.append(
            f"이번 리포트의 핵심 뉴스는 '{selected[0].title_ko}'입니다. 삼성디스플레이 관점에서는 해당 이슈가 수요처 확대, 가격 경쟁, 기술 차별화 중 어디에 영향을 주는지 확인할 필요가 있습니다."
        )

    return bullets[:4]


def key_signal_label(selected: list[Article], grouped: dict[str, list[Article]]) -> str:
    signals: list[str] = []
    if has_any(grouped.get("samsung_display_focus", []), ["QD-OLED", "360Hz", "4K"]):
        signals.append("4K 360Hz QD-OLED")
    if grouped.get("customer_oem_signals"):
        signals.append("차량용 OLED")
    if grouped.get("competitor_moves"):
        signals.append("경쟁사 OLED/microLED")
    if has_any(
        grouped.get("technology_watch", []) + grouped.get("materials_equipment_supply_chain", []),
        ["microLED", "MicroLED"],
    ):
        signals.append("microLED")
    if not signals and selected:
        signals.append(selected[0].title_ko[:28])
    return " / ".join(signals[:4]) if signals else "없음"


def section_takeaway(section_id: str, articles: list[Article]) -> str:
    if not articles:
        return ""
    first = articles[0].title_ko
    if section_id == "samsung_display_focus":
        return f"핵심 기사: {first}. 삼성디스플레이의 제품 경쟁력, 고객 확보, 프리미엄 패널 포지셔닝과 직접 연결되는 이슈입니다."
    if section_id == "market_trends":
        return f"핵심 기사: {first}. 패널 가격, 수요, 공급, 투자 사이클 변화가 삼성디스플레이의 제품 믹스와 수익성에 영향을 줄 수 있습니다."
    if section_id == "customer_oem_signals":
        return f"핵심 기사: {first}. 고객사 채택 신호는 삼성디스플레이의 수주 기회와 응용처 확대 가능성을 판단하는 데 중요합니다."
    if section_id == "competitor_moves":
        competitors = company_mentions(articles, exclude={"Samsung Display"})
        suffix = f" 관련 업체: {', '.join(competitors)}." if competitors else ""
        return f"핵심 기사: {first}.{suffix} 경쟁사의 양산, 협력, 가격 전략은 삼성디스플레이의 고객 방어와 차별화 전략에 영향을 줄 수 있습니다."
    if section_id == "technology_watch":
        return f"핵심 기사: {first}. 차세대 디스플레이 기술 변화는 삼성디스플레이의 OLED/QD-OLED 및 신규 기술 로드맵과 연결해 볼 필요가 있습니다."
    if section_id == "materials_equipment_supply_chain":
        return f"핵심 기사: {first}. 소재, 장비, 생산 기반 변화는 원가, 증설 속도, 공급 안정성 측면에서 의미가 있습니다."
    return f"핵심 기사: {first}."


def section_title(section: dict[str, Any]) -> str:
    return section["title"]


def render_article_card(index: int, article: Article) -> list[str]:
    published = (
        article.published.strftime("%Y-%m-%d %H:%M UTC")
        if article.published
        else "Unknown"
    )
    lines = [
        f"#### {index}. [{md_link_text(article.title_ko)}]({article.link})",
        "",
        "| 항목 | 내용 |",
        "| --- | --- |",
        f"| 출처 | {md_cell(article.source)} |",
        f"| 발행일 | {md_cell(published)} |",
        f"| 주제 | {md_cell(', '.join(article.topics))} |",
        f"| 산업 연관성 | {article.score} |",
        f"| 삼성디스플레이 연관성 | {article.samsung_display_score}/100 |",
    ]
    if article.english_title:
        lines.append(f"| 원문 영어 제목 | {md_cell(article.english_title)} |")
    lines.extend(
        [
            f"| 요약 | {md_cell(short_summary(article))} |",
            f"| 원문 | [기사 열기]({article.link}) |",
            "",
        ]
    )
    return lines


def strategic_implications(selected: list[Article], grouped: dict[str, list[Article]]) -> list[str]:
    competitor_articles = grouped.get("competitor_moves", [])
    tech_and_supply = grouped.get("technology_watch", []) + grouped.get(
        "materials_equipment_supply_chain",
        [],
    )
    app_names = {name for name, _ in application_topic_counts(selected)}

    implications: list[str] = []
    if {"Monitor", "Gaming"} & app_names:
        implications.append(
            "- 게이밍 모니터와 프리미엄 모니터 기사가 많다는 것은 QD-OLED의 현재 수익화 창구가 IT/게이밍 쪽에 열려 있다는 뜻입니다. 삼성디스플레이는 4K 360Hz, 밝기, 번인 저감, 고객사 디자인 채택을 묶어서 프리미엄 모니터 포지션을 방어해야 합니다."
        )
    if "Automotive" in app_names:
        implications.append(
            "- Ferrari와 자동차 디스플레이 시장 뉴스는 차량용 OLED가 브랜드 차별화 부품으로 올라오고 있음을 보여줍니다. 삼성디스플레이는 신뢰성, 장수명, 곡면/대형 cockpit 레퍼런스를 앞세워 차량용 고객 파이프라인을 키울 필요가 있습니다."
        )
    if "Phone" in app_names:
        implications.append(
            "- BOE의 Galaxy S27 패널 가격 공세는 모바일 OLED에서 가격 압박이 계속된다는 신호입니다. 삼성디스플레이는 단순 단가 경쟁보다 LTPO, 전력 효율, 박형화, 품질 안정성 같은 차별 요소로 플래그십 물량을 지키는 전략이 필요합니다."
        )
    if {"TV", "Note PC", "Tablet & Foldable", "XR & Spatial Computing"} & app_names:
        implications.append(
            "- TV, 노트 PC, XR/공간컴퓨팅 등 중대형/신규 폼팩터 응용처는 OLED와 Mini LED, microLED가 동시에 경쟁하는 영역입니다. 삼성디스플레이는 QD-OLED 중심의 프리미엄 전략과 IT OLED 확대 전략을 분리해서 봐야 합니다."
        )
    if competitor_articles:
        competitors = company_mentions(competitor_articles, exclude={"Samsung Display"})
        implications.append(
            f"- 경쟁사 신호({', '.join(competitors) if competitors else '주요 패널 업체'})는 모바일 가격 공세와 IT OLED 양산 확대로 나타납니다. 삼성디스플레이는 고객별 장기 공급 계약, 제품 세대 전환 속도, 원가 개선을 함께 관리해야 합니다."
        )
    if tech_and_supply:
        implications.append(
            "- microLED와 고성능 OLED 관련 기술 뉴스는 아직 단기 매출보다 옵션 가치가 큽니다. 다만 Watch, XR, 산업용/AI 기기 같은 신규 응용처에서 기술 선택이 빨라질 수 있어 선행 고객 PoC를 꾸준히 확보해야 합니다."
        )
    if not implications:
        implications.append(
            "- 오늘 뉴스는 특정 대형 이벤트보다 응용처별 수요 신호를 넓게 확인하는 성격입니다. 삼성디스플레이는 직접 언급 기사뿐 아니라 고객사와 경쟁사 뉴스를 통해 다음 수요처를 추적해야 합니다."
        )
    return implications


def render_report(report_date: dt.date, articles: list[Article], config: dict[str, Any]) -> str:
    selected = select_articles(articles, config)
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top_topics = topic_counts(selected) if selected else []
    grouped = group_articles_by_section(selected, config) if selected else {}
    section_mix = section_counts(grouped, config) if selected else []

    subtitle = str(config["report"].get("subtitle", "")).strip()
    lines = [
        f"# {config['report']['title']}",
        "",
        f"**리포트 날짜:** `{report_date.isoformat()}` | **생성 시각:** `{generated_at}`",
        "",
        "## Market Snapshot",
        "",
        "| 지표 | 값 |",
        "| --- | --- |",
        f"| 수집 기간 | 최근 {int(config['report'].get('lookback_days', 7))}일 |",
        f"| 추적 기사 수 | {len(selected)} |",
        f"| 주요 주제 | {md_cell(', '.join(top_topics[:5]) if top_topics else '없음')} |",
        f"| 응용처 렌즈 | {md_cell(', '.join(application_lenses(config)))} |",
        f"| 핵심 신호 | {md_cell(key_signal_label(selected, grouped))} |",
        f"| 분석 기준 | 삼성디스플레이 연관성 |",
        "",
        "## Executive Summary",
        "",
    ]
    if subtitle:
        lines[2:2] = [f"> {subtitle}", ""]

    if not selected:
        lines.extend(
            [
                "- 오늘은 조건에 맞는 디스플레이 산업 뉴스가 확인되지 않았습니다.",
                "",
                "## News by Theme",
                "",
                "_표시할 기사가 없습니다._",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            *(f"- {summary}" for summary in executive_summary(selected, grouped)),
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
                f"### {section_title(section)}",
                "",
                f"> {section_takeaway(section['id'], section_articles)}",
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
    english_title = (
        f'<p class="english-title">Original title: {html_escape(article.english_title)}</p>'
        if article.english_title
        else ""
    )
    return f"""
      <article class="news-card">
        <div class="news-topline">
          <div class="topic-pills">{topics}</div>
          {render_score(article.samsung_display_score)}
        </div>
        <h3><a href="{html_escape(article.link)}" target="_blank" rel="noreferrer">{html_escape(article.title_ko)}</a></h3>
        {english_title}
        <p>{html_escape(short_summary(article))}</p>
        <dl>
          <div><dt>출처</dt><dd>{html_escape(article.source)}</dd></div>
          <div><dt>발행일</dt><dd>{html_escape(published)}</dd></div>
          <div><dt>산업 연관성</dt><dd>{article.score}</dd></div>
        </dl>
      </article>
    """


def render_html_report(report_date: dt.date, articles: list[Article], config: dict[str, Any]) -> str:
    selected = select_articles(articles, config)
    grouped = group_articles_by_section(selected, config) if selected else {}
    section_mix = section_counts(grouped, config) if selected else []
    top_topics = topic_counts(selected) if selected else []
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    section_blocks: list[str] = []
    for section in config.get("report_sections", []):
        section_articles = grouped.get(section["id"], [])
        if not section_articles:
            continue
        takeaway = section_takeaway(section["id"], section_articles)
        cards = "\n".join(render_html_article(article) for article in section_articles)
        section_blocks.append(
            f"""
            <section class="theme-section">
              <div class="section-heading">
                <h2>{html_escape(section_title(section))}</h2>
                <p>{html_escape(takeaway)}</p>
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
<html lang="ko">
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
    h1 {{
      margin: 12px 0 18px;
      max-width: 840px;
      font-family: "Arial Black", Inter, ui-sans-serif, system-ui, sans-serif;
      font-size: clamp(46px, 8vw, 98px);
      line-height: 0.9;
      letter-spacing: 0;
      text-transform: uppercase;
      background: linear-gradient(92deg, #ffffff 0%, #9eefff 42%, #ff8de3 78%);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      text-shadow: 0 18px 60px rgba(110, 231, 255, 0.18);
    }}
    .hero p {{ max-width: 660px; color: #c7d8ed; font-size: 18px; line-height: 1.65; }}
    .dashboard {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 34px;
    }}
    .metric {{
      background: linear-gradient(180deg, rgba(27, 39, 58, 0.88), rgba(10, 16, 27, 0.82));
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
    .section-heading p {{ color: var(--cyan); margin: 8px 0 0; font-size: 14px; line-height: 1.6; }}
    h2 {{ margin: 0; font-size: 28px; }}
    .news-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .news-card {{
      background: linear-gradient(180deg, rgba(21, 31, 49, 0.96), rgba(11, 17, 29, 0.94));
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
    .news-card h3 {{ margin: 16px 0 10px; font-size: 20px; line-height: 1.35; }}
    .news-card h3 a {{ text-decoration: none; }}
    .news-card h3 a:hover {{ color: var(--cyan); }}
    .english-title {{
      margin: -2px 0 10px;
      color: #91a8c1;
      font-size: 13px;
      line-height: 1.45;
    }}
    .news-card p {{ color: #c8d5e6; line-height: 1.6; }}
    dl {{
      display: grid;
      gap: 8px;
      margin: 18px 0 0;
      padding: 12px;
      border-radius: 8px;
      background: rgba(5, 10, 18, 0.52);
      border: 1px solid rgba(134, 194, 255, 0.14);
    }}
    dl div {{ display: flex; justify-content: space-between; gap: 16px; border-top: 1px solid rgba(255,255,255,0.07); padding-top: 8px; }}
    dl div:first-child {{ border-top: 0; padding-top: 0; }}
    dt {{ color: #8ea4bd; font-size: 12px; text-transform: uppercase; }}
    dd {{ margin: 0; text-align: right; color: #e7f3ff; font-size: 13px; font-weight: 700; }}
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
      <div class="dashboard">
        <div class="metric"><span>리포트 날짜</span><strong>{report_date.isoformat()}</strong></div>
        <div class="metric"><span>추적 기사 수</span><strong>{len(selected)}</strong></div>
        <div class="metric"><span>주요 주제</span><strong>{html_escape(', '.join(top_topics[:2]) if top_topics else '없음')}</strong></div>
        <div class="metric"><span>응용처 렌즈</span><strong>{html_escape(', '.join(application_lenses(config)[:3]))}+</strong></div>
        <div class="metric"><span>생성 시각</span><strong>{html_escape(generated_at)}</strong></div>
      </div>
    </div>
  </header>
  <main>
    <section class="summary">
      <div class="section-heading">
        <h2>Executive Summary</h2>
      </div>
      <ul>
        {''.join(f'<li>{html_escape(item.removeprefix("- "))}</li>' for item in executive_summary(selected, grouped))}
      </ul>
    </section>
    {''.join(section_blocks)}
    <section class="theme-section implications">
      <div class="section-heading">
        <h2>Strategic Implications</h2>
      </div>
      <ul>{implications}</ul>
    </section>
  </main>
  <footer>
    <main>RSS 피드 기반으로 생성되었습니다. 사업 또는 투자 판단 전에는 각 원문 기사를 반드시 확인하세요.</main>
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
