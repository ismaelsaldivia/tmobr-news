#!/usr/bin/env python3
"""TMOBR News builder.

Runs on demand (GitHub Actions, "Run workflow"). Reads the RSS feeds in
feeds.json, asks Claude Haiku to (1) drop off-topic items, (2) classify each
item by region (Brazil, Latin America, global) and topic, (3) pick the lead
stories and (4) propose the chart of the day. Headlines are published in their
original language (EN, PT, ES); nothing is translated. Priority: Brazil, then
Latin America, then global. Every chart figure is checked against the source
text before it is published. Writes index.html and data/news.json.
"""
import calendar, datetime, hashlib, html, json, os, re, sys, time, urllib.request

import feedparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "scripts")
MODEL = "claude-haiku-5-5"
UYT = datetime.timezone(datetime.timedelta(hours=-3))
NOW = time.time()
WINDOW_H = 36
PER_SOURCE = 10
TOPICS = ["business", "telecom", "dc", "tech"]
REGION_RANK = {"BR": 0, "LATAM": 1, "GLOBAL": 2}
PER_REGION = {"brasil": 9, "latam": 6}
PER_SECTION = 4  # global sections; total ~35 items per edition, like the previous version


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


# ---------------------------------------------------------------- editorial gate
# Items from general-interest sources ("core": false in feeds.json) must mention the industry:
# telecom operators and networks, satellites/subsea cables, data centers/cloud, AI and chips,
# electric power for the digital economy, or cybersecurity. Specialist sources ("core": true) skip this gate.
import unicodedata
INDUSTRY = re.compile(r"""telecom|telco|operador|operadora|carrier|\b[456]g\b|\blte\b|fibra optica|fiber|fibre|ftth|banda larga|banda ancha|broadband|espectro|spectrum|anatel|subtel|enacom|\bift\b|ursec|mintic|ministerio das comunicacoes|ministerio tic|satelit|satellite|starlink|kuiper|cabo submarino|cable submarino|subsea|submarine cable|roaming|torres de telecom|cell tower|provedor de internet|provedores|\bisp\b|abrint|\bclaro\b|\bvivo\b|telefonica|\btim\b|v\.tal|brisanet|unifique|giga\+|ligga|tecpar|highline|american tower|tigo|millicom|entel|movistar|america movil|liberty latin|at&t|verizon|t-mobile|vodafone|ericsson|nokia|huawei|data ?center|datacenter|centros? de datos|centros? de dados|hyperscal|hiperescal|colocation|nuvem|\bnube\b|\bcloud\b|\baws\b|azure|equinix|ascenty|scala data|odata|\belea\b|digital realty|inteligencia artificial|\bia\b|\bai\b|a\.i\.|genai|\bllm|openai|anthropic|nvidia|semicondut|semiconduct|\bchips?\b|\bgpus?\b|tsmc|ciberseguran|cybersecur|ciberataque|cyberattack|eletricidade|energia eletrica|electricidad|electricity|transmissao|transmision|power grid|renovav|renovabl|solar|eolica|wind power|baterias?|battery|aneel|leilao de energia|subasta|\bppa\b|megawatts?|\bmw\b|\bgw\b|gigawatts?|internet""")


def norm(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def on_topic(x):
    return bool(INDUSTRY.search(norm(x["title"] + " " + x["summary"])))


# ---------------------------------------------------------------- collect
def collect():
    feeds = json.load(open(os.path.join(HERE, "feeds.json"), encoding="utf-8"))
    items, seen = [], set()
    for f in feeds:
        d = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(f["url"], headers={"User-Agent": "Mozilla/5.0 (TMOBR News aggregator; +https://www.tmobr.com)"})
                d = feedparser.parse(urllib.request.urlopen(req, timeout=30).read())
                break
            except Exception as ex:
                log("FEED RETRY" if attempt < 2 else "FEED FAIL", f["src"], ex)
                time.sleep(4 * (attempt + 1))
        if d is None:
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
            if not title:
                continue
            if "news.google.com" in f["url"]:
                pub = clean((e.get("source") or {}).get("title"))
                for _ in range(2):  # Google News appends " - Publisher" (sometimes twice)
                    if pub and title.endswith(" - " + pub):
                        title = title[: -len(" - " + pub)].rstrip()
                title = re.sub(r"\s+-\s+investingLive$", "", title)
            key = re.sub(r"\W+", "", title.lower())[:80]
            if not title or key in seen or not e.get("link"):
                continue
            seen.add(key)
            summary = clean(e.get("summary"))
            if summary.lower().startswith(title.lower()[:40]):
                summary = ""  # Google News repeats the headline
            item = {
                "id": hashlib.sha1(e["link"].encode()).hexdigest()[:10],
                "src": f["src"], "lang": f["lang"], "sec": f["sec"], "region": f.get("region", "GLOBAL"),
                "title": title, "link": e["link"], "ts": ts,
                "summary": summary[:400], "img": image_of(e),
            }
            if not f.get("core") and not on_topic(item):
                continue  # general-interest source, story outside our industry
            items.append(item)
            n += 1
            if n >= PER_SOURCE:
                break
        log("feed", f["src"], n)
    return items


# ---------------------------------------------------------------- claude
PROMPT = """You are the editor of TMOBR News, a non-profit aggregator of business, telecom, data center/energy and technology news for industry professionals. Editorial priority: (1) Brazil's industry, (2) Latin America's industry, (3) global industry. Headlines are published in their original language; do NOT translate anything.

Below are today's candidate items, one per line: id | language | region hint | section hint | source | headline | excerpt.

Call the submit tool with a JSON object with these keys:
- "picks": array with ONE object per item worth publishing: {"id": item id, "region": "BR" (the story is about Brazil or a Brazilian company/market) or "LATAM" (Latin America outside Brazil, or the region as a whole) or "GLOBAL" (anything else) — decide by the story, not by the language or the source: a Spanish- or English-language item about Brazil is "BR"; an item from a Brazilian or Latin American outlet about China, Europe or the US is "GLOBAL"; "sec": "business", "telecom", "dc" (data centers and energy) or "tech"; "story": short lowercase English slug of 3 to 6 words naming the underlying event — items about the same event, in any language or from any source, MUST get exactly the same slug}. Include every item you keep; do not skip fields. TMOBR News covers ONE industry: telecommunications (operators, fixed and mobile networks, fiber, 5G, spectrum, towers, ISPs, satellites, subsea cables, regulators and telecom policy), data centers and cloud, artificial intelligence and semiconductors, electric power and energy infrastructure that serves the digital economy, cybersecurity of networks and companies, and the business side of these sectors (deals, M&A, investments, financing, earnings, executives of companies in these sectors). Keep an item ONLY if its main subject is this industry. Drop everything else even if it comes from a business or technology outlet: general macroeconomics, central banks, fiscal policy, politics, banks and finance not tied to the sector, retail and Black Friday, betting and gambling (bets, apuestas, ludopatía), consumer gadget reviews and deals, games, food, travel, culture, TV programming, sports, health, crime, oil and gas, mining, agribusiness, and M&A of companies outside the sector. When in doubt, drop it. If several items report the same story (in any language), keep only the most informative one, preferring the Brazilian or Latin American source. Expect to keep roughly 40 to 60 items.
- "top": array of 4 kept ids, the most important stories of the day for this audience, most important first. Apply the editorial priority: Brazilian stories first, then Latin American, then global, as long as they have substance (deals, capacity, regulation, financing).
- "chart": the most striking figure of the day for this audience, preferably from a Brazilian or Latin American item, or null. Object: {"title": short headline stating the finding, written in the same language as the source item, "value": the number exactly as written in the source text, "total": a second number from the SAME source text it should be compared against, or "", "value_label": short label for value, "total_label": short label for total or "", "id": source item id}. Use only numbers that appear literally in that item's headline or excerpt. Copy each value WITH its currency symbol and unit exactly as written (for example "$45 billion", "R$ 25 bilhões", "960MW", "4,5%"), never the bare number.
- "numbers": array of up to 4 other striking figures: {"value": number exactly as written in the source text, "label": label of at most 10 words in the same language as the source item, "id": source item id}. Each from a different item. Use only numbers that appear literally in that item's headline or excerpt.

Items:
"""


SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {"type": "array", "description": "One object per item worth publishing (expect 40 to 60).",
                  "items": {"type": "object", "properties": {
                      "id": {"type": "string"},
                      "region": {"type": "string", "enum": ["BR", "LATAM", "GLOBAL"]},
                      "sec": {"type": "string", "enum": ["business", "telecom", "dc", "tech"]},
                      "story": {"type": "string"}},
                      "required": ["id", "region", "sec", "story"]}},
        "top": {"type": "array", "items": {"type": "string"}, "description": "4 ids, most important first."},
        "chart": {"type": ["object", "null"], "properties": {
            "title": {"type": "string"}, "value": {"type": "string"}, "total": {"type": "string"},
            "value_label": {"type": "string"}, "total_label": {"type": "string"}, "id": {"type": "string"}}},
        "numbers": {"type": "array", "items": {"type": "object", "properties": {
            "value": {"type": "string"}, "label": {"type": "string"}, "id": {"type": "string"}},
            "required": ["value", "label", "id"]}},
    },
    "required": ["picks", "top", "chart", "numbers"],
}


def call_claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    body = json.dumps({
        "model": MODEL, "max_tokens": 16000,
        "tools": [{"name": "submit", "description": "Submit the edition: every selected item in picks, the 4 top ids, the chart and the numbers.",
                   "input_schema": SCHEMA}],
        "tool_choice": {"type": "tool", "name": "submit"},
        "messages": [{"role": "user", "content": prompt}],
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


def ask_claude(items):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log("NO ANTHROPIC_API_KEY: publishing English items only, no chart")
        return None
    lines = [f'{x["id"]} | {x["lang"]} | {x["region"]} | {x["sec"]} | {x["src"]} | {x["title"]} | {x["summary"][:220]}' for x in items]
    return call_claude(PROMPT + "\n".join(lines))


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
    return x["title"]


def card(x, size="std", pic=False):
    tag = f'<span class="lang">{x["lang"]}</span>' if x["lang"] != "EN" else ""
    p = ""
    if pic and x.get("img"):
        p = (f'<a href="{esc(x["link"])}" target="_blank" rel="noopener" class="pic"><img src="{esc(x["img"])}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.remove()">'
             f'<span class="credit">Image: {esc(x["src"])}</span></a>')
    dek = x["summary"]
    if dek and size in ("lead", "side"):
        dek = dek if len(dek) <= 220 else dek[:217].rsplit(" ", 1)[0] + "…"
        d = f'<p class="dek">{esc(dek)}</p>'
    else:
        d = ""
    hl = f' lang="{x["lang"].lower()}"'
    return (f'<article class="card {size}"{hl}>{p}<div class="meta"><span class="src">{esc(x["src"])}</span>{tag}'
            f'<span class="ago" data-ts="{x["ts"]}"></span></div>'
            f'<h3><a href="{esc(x["link"])}" target="_blank" rel="noopener">{esc(headline(x))}</a></h3>{d}'
            f'<a class="readat" href="{esc(x["link"])}" target="_blank" rel="noopener">{esc(x["src"])} ↗</a></article>')


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
    for x in items:  # real language of the headline (some sources mix PT and ES)
        if x["lang"] != "EN":
            x["lang"] = detect_lang(x["title"] + " " + x["summary"], x["lang"])
    items.sort(key=lambda x: -x["ts"])
    byid = {x["id"]: x for x in items}
    ai = ask_claude(items)

    if ai:
        picks = [p for p in (ai.get("picks") or []) if isinstance(p, dict) and p.get("id") in byid]
        log("AI keys", sorted(ai.keys()), "| picks", len(picks), "| top", len(ai.get("top") or []))
        keep = set(p["id"] for p in picks)
        story = {}
        for p in picks:
            x = byid[p["id"]]
            if p.get("region") in REGION_RANK:
                x["region"] = p["region"]
            if p.get("sec") in TOPICS:
                x["sec"] = p["sec"]
            if p.get("story"):
                story[p["id"]] = p["story"]
            # safety net: a story whose headline is about Brazil goes to Brazil
            if x["region"] != "BR" and re.search(r"\bbrasil|\bbrazil|brasileñ|brasileir", norm(x["title"])):
                x["region"] = "BR"
        pool = [x for x in items if x["id"] in keep]
        top = [byid[i] for i in ai.get("top", []) if i in byid and byid[i] in pool]
        # one item per story: top picks first, then Brazil/LatAm, then items with a picture, then newest
        order = sorted(pool, key=lambda x: (0 if x in top else 1, REGION_RANK[x["region"]], 0 if x.get("img") else 1, -x["ts"]))
        seen_story, dedup = set(), set()
        for x in order:
            slug = re.sub(r"\W+", " ", str(story.get(x["id"], x["id"])).lower()).strip()
            if slug in seen_story:
                log("duplicate story dropped", x["src"], x["title"][:60])
                continue
            seen_story.add(slug); dedup.add(x["id"])
        pool = [x for x in pool if x["id"] in dedup]
        # second safety net: near-identical headlines (same story from several outlets)
        def toks(t):
            return {w for w in norm(t).split() if len(w) > 3}
        kept_t, final = [], set()
        for x in sorted(pool, key=lambda x: (0 if x in top else 1, REGION_RANK[x["region"]], 0 if x.get("img") else 1, -x["ts"])):
            t = toks(x["title"])
            if any(len(t & k) / max(1, min(len(t), len(k))) >= 0.6 for k in kept_t):
                log("similar headline dropped", x["src"], x["title"][:60]); continue
            kept_t.append(t); final.add(x["id"])
        pool = [x for x in pool if x["id"] in final]
        top = [x for x in top if x["id"] in final]
        chart = ai.get("chart")
        if not isinstance(chart, dict):
            chart = None
        if chart and not (chart.get("id") in byid and appears(chart.get("value"), byid[chart["id"]])):
            log("chart rejected (figure not found in source)", chart)
            chart = None
        if chart and chart.get("total") and not appears(chart["total"], byid[chart["id"]]):
            chart["total"] = ""
        numbers, used = [], set()
        for n in ai.get("numbers") or []:
            if isinstance(n, dict) and n.get("id") in byid and n["id"] not in used and appears(n.get("value"), byid[n["id"]]):
                numbers.append(n); used.add(n["id"])
            else:
                log("number rejected", n)
        numbers = numbers[:4]
    else:
        pool = list(items)
        top, chart, numbers = [], None, []

    if not pool:
        log("Empty pool; keeping the previous edition")
        sys.exit(1)
    # editorial priority for the front page: Brazil, then Latin America, then global
    rank = {x["id"]: k for k, x in enumerate(top)}
    top.sort(key=lambda x: (REGION_RANK[x["region"]], rank[x["id"]]))
    if len(top) < 4:
        extra = sorted([x for x in pool if x not in top], key=lambda x: (REGION_RANK[x["region"]], 0 if x.get("img") else 1, -x["ts"]))
        top += extra[: 4 - len(top)]
    lead = next((x for x in top if x.get("img")), top[0])
    side = [x for x in top if x is not lead][:3]
    used = {lead["id"]} | {x["id"] for x in side}

    S = {"lead": card(lead, "lead", pic=True), "lead2": "".join(card(x, "side") for x in side)}

    def block(lst, n):
        lst = lst[:n]
        withpic = [x for x in lst if x.get("img")]
        lst = withpic[:2] + [x for x in lst if x not in withpic[:2]]  # two pictures on top, rest by time
        used.update(x["id"] for x in lst)
        return "".join(card(x, pic=(k < 2)) for k, x in enumerate(lst)) or '<p class="dek">—</p>'

    S["brasil"] = block([x for x in pool if x["region"] == "BR" and x["id"] not in used], PER_REGION["brasil"])
    S["latam"] = block([x for x in pool if x["region"] == "LATAM" and x["id"] not in used], PER_REGION["latam"])
    for sec in TOPICS:
        S[sec] = block([x for x in pool if x["region"] == "GLOBAL" and x["sec"] == sec and x["id"] not in used], PER_SECTION)
    S["wire"] = "".join(wire_li(x) for x in pool[:15])
    S["chart"] = chart_html(chart, numbers, byid)
    published = [x for x in pool if x["id"] in used]
    srcs = sorted({x["src"] for x in published}, key=str.lower)
    S["sources"] = " · ".join(esc(s) for s in srcs)
    S["nsources"] = str(len(srcs))
    now = datetime.datetime.now(UYT)
    S["date"] = now.strftime("%A, %-d %B %Y")
    S["edition"] = now.strftime("%H:%M")
    S["count"] = str(len(published))

    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    for k, v in S.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    if "{{" in tpl:
        log("Template placeholders left unfilled"); sys.exit(1)
    open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8").write(tpl)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    json.dump({"updated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "edition": S["edition"], "curated": bool(ai), "chart": chart, "numbers": numbers,
               "items": published}, open(os.path.join(ROOT, "data", "news.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("published", len(published), "items from", len(srcs), "sources; curated:", bool(ai),
        "| BR", sum(x["region"] == "BR" for x in published), "LATAM", sum(x["region"] == "LATAM" for x in published),
        "GLOBAL", sum(x["region"] == "GLOBAL" for x in published))


if __name__ == "__main__":
    main()
