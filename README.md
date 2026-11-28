# TMOBR News · Business & Technology

Non-profit aggregator of curated industry headlines published at https://www.tmobr.com.
Every item links to its original publisher. Headlines are published in their original language (English, Portuguese, Spanish). Editorial priority: Brazil, then Latin America, then global.

- `scripts/feeds.json`: sources (RSS).
- `scripts/build.py`: builds `index.html` and `data/news.json`.
- `.github/workflows/update.yml`: runs on demand only (Actions → Update TMOBR News → Run workflow; linked from the "Generate new edition" button on the site).
- Secret `TMOBR_NEWS` (Anthropic API key): used for filtering, region/topic classification and the chart of the day (Claude Haiku 5.5).

Weather: Open-Meteo.com (CC BY 4.0). Market data: TradingView widget.
Publishers who prefer not to be included: contacto@tmobr.com.
