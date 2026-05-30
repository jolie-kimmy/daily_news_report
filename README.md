# Display Weekly Report

Display-industry report generator.

This repository collects display-industry news from RSS feeds, filters relevant
articles, groups them by topic, scores Samsung Display relevance, and writes a
clean Markdown report under `reports/`.

## Scope

The first version focuses on:

- OLED, LCD, microLED, QD-OLED, Mini LED, and foldable displays
- Display panel makers such as Samsung Display, LG Display, BOE, AUO, Innolux,
  Tianma, Visionox, Japan Display, and Sharp
- Supply chain, panel pricing, capex, capacity, demand, and application trends
- Samsung Display relevance scoring for each collected article
- Thematic grouping by Samsung Display focus, market trends, customers,
  competitors, technology, and supply chain signals

## Repository Layout

```text
.
├─ .github/workflows/daily-report.yml
├─ reports/
├─ scripts/generate_report.py
├─ sources.yaml
└─ README.md
```

## Local Usage

```powershell
python scripts/generate_report.py
```

The script creates a report at:

```text
reports/YYYY-MM-DD.md
```

To generate a report for a specific date:

```powershell
python scripts/generate_report.py --date 2026-05-30
```

## GitHub Actions

The workflow runs every day at 09:00 Korea Standard Time and commits a new
report when there are relevant news items.

Required repository setting:

- `Settings > Actions > General > Workflow permissions`
- Enable `Read and write permissions`

Optional repository secret:

- `OPENAI_API_KEY`: reserved for a future richer summarization step

## Next Improvements

- Add AI-generated executive summaries
- Add company-level impact scoring
- Add weekly roll-up reports
- Publish the latest report with GitHub Pages
- Send daily reports to email, Slack, or Discord
