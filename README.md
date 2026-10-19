# TMOBR News · Business & Technology

Non-profit aggregator of curated industry headlines published at https://www.tmobr.com.
Every item links to its original publisher. Portuguese and Spanish headlines are machine-translated into English for reference.

- `scripts/feeds.json`: sources (RSS).
- `scripts/build.py`: builds `index.html` and `data/news.json`.
- `.github/workflows/update.yml`: runs at 07:00 and 17:00 Montevideo (10:00 and 20:00 UTC).
- Secret `ANTHROPIC_API_KEY`: used for filtering, translation and the chart of the day (Claude Haiku).

Weather: Open-Meteo.com (CC BY 4.0). Market data: TradingView widget.
Publishers who prefer not to be included: contacto@tmobr.com.
