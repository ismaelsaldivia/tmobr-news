#!/usr/bin/env python3
"""TMOBR News builder.

Runs twice a day (GitHub Actions). Reads the RSS feeds in feeds.json, asks
Claude Haiku to (1) drop off-topic items, (2) translate Portuguese/Spanish
headlines into English, (3) pick the lead stories and (4) propose the chart of
the day. Every chart figure is checked against the source text before it is
published. Writes index.html and data/news.json.
"""
import calendar, datetime, hashlib, html, json, os, re, sys, time, urllib.request

import feedparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "scripts")
MODEL = "claude-haiku-4-5-20251001"
UYT = datetime.timezone(datetime.timedelta(hours=-3))
NOW = time.time()
WINDOW_H = 36
PER_SOURCE = 10
LANGNAME = {"PT": "Portuguese", "ES": "Spanish"}
SECTIONS = ["business", "telecom", "dc", "tech", "brazil", "latam"]
PER_SECTION = 6


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def esc(s):
    return html.escape(s or "", quote=True)


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def image_of(e):
    for k in ("media_content", "media_thumbnail"):
        for m in e.get(k) or []:
            u = m.get("url")
            if u and not u.lower().endswith((".mp3", ".mp4")):
                return u
    for l in e.get("links", []):
        if l.get("rel") == "enclosure" and "image" in (l.get("type") or "image"):
            return l.get("href")
    body = ((e.get("content") or [{}])[0].get("value", "")) + e.get("summary", "")
    m = re.search(r'<img[^>]+src="(https?://[^"]+)"', body)
    return m.group(1) if m else None


# ---------------------------------------------------------------- collect
def collect():
    feeds = json.load(open(os.path.join(HERE, "feeds.json"), encoding="utf-8"))
    items, seen = [], set()
    for f in feeds:
        try:
            req = urllib.request.Request(f["url"], headers={"User-Agent": "Mozilla/5.0 (TMOBR News aggregator; +https://www.tmobr.com)"})
            d = feedparser.parse(urllib.request.urlopen(req, timeout=30).read())
        except Exception as ex:
            log("FEED FAIL", f["src"], ex)
            continue
        n = 0
        for e in d.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            if not t:
                continue
            ts = calendar.timegm(t)
            if ts > NOW + 3600 or NOW - ts > WINDOW_H * 3600:
                continue
            title = clean(e.get("title"))
            if f["src"] == "investingLive":
                title = re.sub(r"\s+-\s+investingLive$", "", title)
            key = re.sub(r"\W+", "", title.lower())[:80]
            if not title or key in seen or not e.get("link"):
                continue
            seen.add(key)
            summary = clean(e.get("summary"))
            if summary.lower().startswith(title.lower()[:40]):
                summary = ""  # Google News repeats the headline
            items.append({
                "id": hashlib.sha1(e["link"].encode()).hexdigest()[:10],
                "src": f["src"], "lang": f["lang"], "sec": f["sec"],
                "title": title, "link": e["link"], "ts": ts,
                "summary": summary[:400], "img": image_of(e),
            })
            n += 1
            if n >= PER_SOURCE:
                break
        log("feed", f["src"], n)
    return items


# ---------------------------------------------------------------- claude
PROMPT = """You are the editor of TMOBR News, a non-profit aggregator of business, telecom, data center/energy and technology news for industry professionals in the Americas.

Below are today's candidate items, one per line: id | language | section | source | headline | excerpt.

Return ONLY a JSON object with these keys:
- "keep": array of ids worth publishing. Keep business, markets, macroeconomics, central banks, trade, commodities, energy markets, telecom, satellites, subsea cables, data centers, energy for data centers, AI, semiconductors, technology policy and corporate deals. Drop lifestyle, culture, sports, crime, celebrity, generic domestic politics, sponsored content, events/webinars and market-session wraps that only list prices. If several items report the same story (in any language), keep only the most informative one.
- "en": object mapping EVERY kept PT or ES id to {"o": the first five words of that item's ORIGINAL headline copied exactly, "lang": "PT" or "ES" (the real language of that headline), "t": English headline, "x": English one-sentence excerpt of at most 25 words, or "" if there is no excerpt}. Translate faithfully; do not add facts. Double-check that each translation belongs to its own id.
- "story": object mapping EVERY kept id to a short lowercase English slug (3 to 6 words) naming the underlying event. Items that report the same event, in any language or from any source, MUST get exactly the same slug.
- "top": array of 4 kept ids, the most important stories of the day for this audience, most important first. Prefer items with substance (deals, capacity, regulation, financing).
- "chart": the most striking figure of the day for this audience, taken from an EN (English-language) item, or null. Object: {"title": short English headline stating the finding, "value": the number exactly as written in the source text, "total": a second number from the SAME source text it should be compared against, or "", "value_label": short label for value, "total_label": short label for total or "", "id": source item id}. Use only numbers that appear literally in that item's headline or excerpt. Copy each value WITH its currency symbol and unit exactly as written (for example "$45 billion", "960MW", "4.5%"), never the bare number.
- "numbers": array of up to 4 other striking figures, each taken from an EN (English-language) item: {"value": number exactly as written in the source text, "label": English label of at most 10 words, "id": source item id}. Each from a different item. Use only numbers that appear literally in that item's headline or excerpt.

Items:
"""


TRANSLATE = """Translate each headline and excerpt below from Portuguese or Spanish into English for TMOBR News. Translate faithfully; do not add facts.
Return ONLY a JSON object mapping EVERY id to {"o": the first five words of the ORIGINAL headline copied exactly, "t": English headline, "x": English one-sentence excerpt of at most 25 words, or "" if there is no excerpt}.

Items (id | headline | excerpt):
"""


def call_claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    body = json.dumps({
        "model": MODEL, "max_tokens": 16000,
        "tools": [{"name": "submit", "description": "Return the requested JSON object.",
                   "input_schema": {"type": "object", "additionalProperties": True}}],
        "tool_choice": {"type": "tool", "name": "submit"},
        "messages": [{"role": "user", "content": prompt.replace("Return ONLY a JSON object", "Call the submit tool with a JSON object")}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=240).read())
        u = r.get("usage", {})
        log("claude usage", u.get("input_tokens"), "in /", u.get("output_tokens"), "out", r.get("stop_reason"))
        for blk in r.get("content", []):
            if blk.get("type") == "tool_use" and isinstance(blk.get("input"), dict):
                return blk["input"]
        log("CLAUDE FAIL no tool output")
        return None
    except Exception as ex:
        log("CLAUDE FAIL", ex)
        return None


def translate_missing(items):
    if not items:
        return {}
    lines = [f'{x["id"]} | {x["title"]} | {x["summary"][:220]}' for x in items]
    return call_claude(TRANSLATE + "\n".join(lines)) or {}


def ask_claude(items):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log("NO ANTHROPIC_API_KEY: publishing English items only, no chart")
        return None
    lines = [f'{x["id"]} | {x["lang"]} | {x["sec"]} | {x["src"]} | {x["title"]} | {x["summary"][:220]}' for x in items]
    return call_claude(PROMPT + "\n".join(lines))
    body = json.dumps({
        "model": MODEL, "max_tokens": 12000,
        "messages": [{"role": "user", "content": PROMPT + "\n".join(lines)}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=240).read())
        text = "".join(b.get("text", "") for b in r.get("content", []))
        u = r.get("usage", {})
        log("claude usage", u.get("input_tokens"), "in /", u.get("output_tokens"), "out")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0))
    except Exception as ex:
        log("CLAUDE FAIL", ex)
        return None


PT_HINTS = ("ção", "ções", "ão ", "ões", "ã", "õ", " não ", " após ", " em ", " do ", " da ", " dos ", " das ", " para o ", " com ", "ê", "lh")
ES_HINTS = ("ción", "ciones", "ñ", " el ", " los ", " las ", " del ", " para el ", " con ", " una ", " según ", " y ")


def detect_lang(text, default):
    t = " " + (text or "").lower() + " "
    pt = sum(t.count(h) for h in PT_HINTS)
    es = sum(t.count(h) for h in ES_HINTS)
    if pt > es:
        return "PT"
    if es > pt:
        return "ES"
    return default


# ---------------------------------------------------------------- verify figures
def digits(s):
    return re.sub(r"[^\d.]", "", (s or "").replace(",", ""))


def appears(value, item):
    """True if the figure is literally present in the item's headline/excerpt."""
    if not value or not re.search(r"\d", value):
        return False
    text = item["title"] + " " + item["summary"]
    if value in text:
        return True
    dv = digits(value)
    return bool(dv) and any(digits(tok) == dv for tok in re.findall(r"[\d][\d.,]*", text))


def num(s):
    try:
        return float(digits(s))
    except ValueError:
        return None


# ---------------------------------------------------------------- render
def headline(x):
    return x.get("title_en") or x["title"]


def card(x, size="std", pic=False):
    tr = x["lang"] != "EN"
    tag = f'<span class="lang">{x["lang"]}</span>' if tr else ""
    note = (f'<div class="trnote">Headline translated from {LANGNAME[x["lang"]]} · original: “{esc(x["title"])}”</div>' if tr else "")
    p = ""
    if pic and x.get("img"):
        p = (f'<a href="{esc(x["link"])}" target="_blank" rel="noopener" class="pic"><img src="{esc(x["img"])}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.remove()">'
             f'<span class="credit">Image: {esc(x["src"])}</span></a>')
    dek = x.get("excerpt_en") if tr else x["summary"]
    if dek and size in ("lead", "side"):
        dek = dek if len(dek) <= 220 else dek[:217].rsplit(" ", 1)[0] + "…"
        d = f'<p class="dek">{esc(dek)}</p>'
    else:
        d = ""
    return (f'<article class="card {size}">{p}<div class="meta"><span class="src">{esc(x["src"])}</span>{tag}'
            f'<span class="ago" data-ts="{x["ts"]}"></span></div>'
            f'<h3><a href="{esc(x["link"])}" target="_blank" rel="noopener">{esc(headline(x))}</a></h3>{d}{note}'
            f'<a class="readat" href="{esc(x["link"])}" target="_blank" rel="noopener">Read at {esc(x["src"])} ↗</a></article>')


def wire_li(x):
    tag = f' <span class="lang sm">{x["lang"]}</span>' if x["lang"] != "EN" else ""
    return (f'<li><span class="wt" data-ts="{x["ts"]}"></span><a href="{esc(x["link"])}" target="_blank" rel="noopener">{esc(headline(x))}</a>'
            f'<span class="ws">{esc(x["src"])}{tag}</span></li>')


def chart_html(chart, numbers, byid):
    if not chart and not numbers:
        return ""
    out = ['    <section class="cod">', '      <div class="rh">Chart of the day</div>']
    srcs = []
    if chart:
        x = byid[chart["id"]]
        srcs.append(x)
        out.append(f'      <h4>{esc(chart["title"])}</h4>')
        v, t = num(chart["value"]), num(chart.get("total", ""))
        if v is not None and t and t > 0 and v <= t:
            pct = max(1, round(100 * v / t))
            out.append(f'      <div class="bar2">{esc(chart.get("value_label",""))}'
                       f'<div class="track"><i style="width:{pct}%"></i></div>'
                       f'<span style="color:#C8D2E3;font-weight:500">{esc(chart["value"])} vs {esc(chart["total"])} {esc(chart.get("total_label",""))} · {pct}%</span></div>')
        else:
            out.append(f'      <div class="num" style="margin-bottom:6px"><b style="font-size:44px">{esc(chart["value"])}</b><span>{esc(chart.get("value_label",""))}</span></div>')
    if numbers:
        out.append('      <div class="nums">')
        for n in numbers:
            out.append(f'        <div class="num"><b>{esc(n["value"])}</b><span>{esc(n["label"])}</span></div>')
            if byid[n["id"]] not in srcs:
                srcs.append(byid[n["id"]])
        out.append('      </div>')
    links = " · ".join(f'<a href="{esc(s["link"])}" target="_blank" rel="noopener">{esc(s["src"])} ↗</a>' for s in srcs)
    out.append(f'      <div class="srcl">Sources: {links}</div>')
    out.append('    </section>')
    return "\n".join(out)


def main():
    items = collect()
    if len(items) < 20:
        log("Too few items collected; keeping the previous edition")
        sys.exit(1)
    items.sort(key=lambda x: -x["ts"])
    byid = {x["id"]: x for x in items}
    ai = ask_claude(items)

    if ai:
        keep = set(i for i in ai.get("keep", []) if i in byid)
        import unicodedata
        def words(t):
            t = unicodedata.normalize("NFKD", (t or "").lower())
            t = "".join(c for c in t if not unicodedata.combining(c))
            return re.sub(r"[^\w ]+", " ", t).split()
        en = dict(ai.get("en") or {})
        missing = [byid[i] for i in keep if byid[i]["lang"] != "EN" and not (isinstance(en.get(i), dict) and en[i].get("t"))]
        if missing:
            log("second translation pass for", len(missing), "items")
            en.update(translate_missing(missing))
        for i, tr in en.items():
            if i in byid and byid[i]["lang"] != "EN" and isinstance(tr, dict) and tr.get("t"):
                o = [w for w in words(tr.get("o"))[:4] if len(w) > 2]
                head = set(words(byid[i]["title"])[:10])
                if not o or sum(w in head for w in o) < max(1, len(o) - 1):
                    log("translation rejected (does not match its original)", i, tr.get("o"))
                    continue
                byid[i]["lang"] = detect_lang(byid[i]["title"] + " " + byid[i]["summary"], byid[i]["lang"])
                byid[i]["sec"] = "brazil" if byid[i]["lang"] == "PT" else "latam"
                byid[i]["title_en"] = tr["t"].strip()
                byid[i]["excerpt_en"] = (tr.get("x") or "").strip()
        pool = [x for x in items if x["id"] in keep and (x["lang"] == "EN" or x.get("title_en"))]
        top = [byid[i] for i in ai.get("top", []) if i in byid and byid[i] in pool]
        # one item per story: top picks first, then items with a picture, then newest
        story = ai.get("story") or {}
        order = sorted(pool, key=lambda x: (0 if x in top else 1, 0 if x.get("img") else 1, -x["ts"]))
        seen_story, dedup = set(), set()
        for x in order:
            slug = re.sub(r"\W+", " ", str(story.get(x["id"], x["id"])).lower()).strip()
            if slug in seen_story:
                log("duplicate story dropped", x["src"], x["title"][:60])
                continue
            seen_story.add(slug); dedup.add(x["id"])
        pool = [x for x in pool if x["id"] in dedup]
        top = [x for x in top if x["id"] in dedup]
        chart = ai.get("chart")
        if chart and chart.get("id") in byid and byid[chart["id"]]["lang"] != "EN":
            chart = None
        if chart and not (chart.get("id") in byid and appears(chart.get("value"), byid[chart["id"]])):
            log("chart rejected (figure not found in source)", chart)
            chart = None
        if chart and chart.get("total") and not appears(chart["total"], byid[chart["id"]]):
            chart["total"] = ""
        numbers, used = [], set()
        for n in ai.get("numbers") or []:
            if n.get("id") in byid and byid[n["id"]]["lang"] == "EN" and n["id"] not in used and appears(n.get("value"), byid[n["id"]]):
                numbers.append(n); used.add(n["id"])
            else:
                log("number rejected", n)
        numbers = numbers[:4]
    else:
        pool = [x for x in items if x["lang"] == "EN"]
        top, chart, numbers = [], None, []

    if not pool:
        log("Empty pool; keeping the previous edition")
        sys.exit(1)
    if not top:
        top = [x for x in pool if x["sec"] in ("dc", "telecom", "business")][:4]
    # lead must have an image when possible
    lead = next((x for x in top if x.get("img")), top[0])
    side = [x for x in top if x is not lead][:3]
    used = {lead["id"]} | {x["id"] for x in side}

    S = {"lead": card(lead, "lead", pic=True), "lead2": "".join(card(x, "side") for x in side)}
    for sec in SECTIONS:
        lst = [x for x in pool if x["sec"] == sec and x["id"] not in used][:PER_SECTION]
        lst.sort(key=lambda x: 0 if x.get("img") else 1)  # a picture first
        used |= {x["id"] for x in lst}
        S[sec] = "".join(card(x, pic=(k < 2)) for k, x in enumerate(lst)) or '<p class="dek">No stories in this edition.</p>'
    S["wire"] = "".join(wire_li(x) for x in pool[:15])
    S["chart"] = chart_html(chart, numbers, byid)
    published = [x for x in pool if x["id"] in used]
    srcs = sorted({x["src"] for x in published}, key=str.lower)
    S["sources"] = " · ".join(esc(s) for s in srcs)
    S["nsources"] = str(len(srcs))
    now = datetime.datetime.now(UYT)
    S["date"] = now.strftime("%A, %-d %B %Y")
    S["edition"] = "07:00" if now.hour < 12 else "17:00"

    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    for k, v in S.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    if "{{" in tpl:
        log("Template placeholders left unfilled"); sys.exit(1)
    open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8").write(tpl)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    json.dump({"updated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "edition": S["edition"], "translated": bool(ai), "chart": chart, "numbers": numbers,
               "items": published}, open(os.path.join(ROOT, "data", "news.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("published", len(published), "items from", len(srcs), "sources; translated:", bool(ai))


if __name__ == "__main__":
    main()
