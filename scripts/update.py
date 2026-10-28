#!/usr/bin/env python3
"""Fetch the latest PCSO results, merge them into site/pcso.json and rebuild the static pages.

Source: the daily results pages of GMA News Online (gmanetwork.com/news/lotto/), whose
robots.txt allows this section. One page per day is read, with a pause between requests.

Usage:
  python scripts/update.py              fetch the last DAYS days, merge, rebuild
  python scripts/update.py --build-only rebuild pages from site/pcso.json without fetching
  python scripts/update.py --from-file f.json   merge results saved as {slug: [[type, nums, prize], ...]}
"""
import datetime as dt
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DATA = SITE / "pcso.json"
DOMAIN = "https://phlottoodds.com"
DAYS = 10
USER_AGENT = "PHLottoOddsBot/1.0 (+https://phlottoodds.com/about.html)"
MANILA = dt.timezone(dt.timedelta(hours=8))
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]

GAMES = [
    # id, name, slug, kind, N or digits, price, draw days, tagalog/alt names
    dict(id="658", name="Ultra Lotto 6/58", slug="ultra-lotto-6-58", kind="pick", N=58, price=25, days="Tuesday, Friday and Sunday", alt="6/58"),
    dict(id="655", name="Grand Lotto 6/55", slug="grand-lotto-6-55", kind="pick", N=55, price=25, days="Monday, Wednesday and Saturday", alt="6/55"),
    dict(id="649", name="Super Lotto 6/49", slug="super-lotto-6-49", kind="pick", N=49, price=25, days="Tuesday, Thursday and Sunday", alt="6/49"),
    dict(id="645", name="Mega Lotto 6/45", slug="mega-lotto-6-45", kind="pick", N=45, price=25, days="Monday, Wednesday and Friday", alt="6/45"),
    dict(id="642", name="Lotto 6/42", slug="lotto-6-42", kind="pick", N=42, price=25, days="Tuesday, Thursday and Saturday", alt="6/42"),
    dict(id="6d", name="6D Lotto", slug="6d-lotto", kind="digits", k=6, price=20, days="Tuesday, Thursday and Saturday", alt="6-Digit"),
    dict(id="4d", name="4D Lotto", slug="4d-lotto", kind="digits", k=4, price=10, days="Monday, Wednesday and Friday", alt="4-Digit"),
    dict(id="3d", name="3D Swertres", slug="swertres-3d", kind="digits", k=3, price=10, days="every day at 2 PM, 5 PM and 9 PM", alt="Suertres"),
    dict(id="2d", name="2D EZ2", slug="ez2-2d", kind="ez2", price=10, days="every day at 2 PM, 5 PM and 9 PM", alt="EZ2"),
]
BY_ID = {g["id"]: g for g in GAMES}
TIME_ORDER = {"11AM": 0, "11:30AM": 1, "12:30PM": 2, "2PM": 3, "4PM": 4, "5PM": 5, "9PM": 6}


# ---------------------------------------------------------------- source parsing
def game_from_label(label):
    """Map a GMA label such as 'Swertres 9PM' or 'Grand Lotto 6/55' to (game id, draw time)."""
    s = re.sub(r"\s+", " ", label).strip()
    m = re.search(r"(\d{1,2}(?::\d\d)?\s*[AP]M)", s, re.I)
    when = m.group(1).replace(" ", "").upper() if m else ""
    for code in ("6/58", "6/55", "6/49", "6/45", "6/42"):
        if code in s:
            return code.replace("/", ""), ""
    low = s.lower()
    if low.startswith("6d"):
        return "6d", ""
    if low.startswith("4d"):
        return "4d", ""
    if "swertres" in low or "suertres" in low or low.startswith("3d"):
        return "3d", when
    if low.startswith("2d") or "ez2" in low:
        return "2d", when
    return None, ""


def valid(gid, nums):
    g = BY_ID[gid]
    if g["kind"] == "pick":
        return len(nums) == 6 and len(set(nums)) == 6 and all(1 <= x <= g["N"] for x in nums)
    if g["kind"] == "digits":
        return len(nums) == g["k"] and all(0 <= x <= 9 for x in nums)
    return len(nums) == 2 and all(1 <= x <= 31 for x in nums)


def to_rows(date_iso, entries):
    """entries: [[label, numbers text, prize text], ...] -> {gid: [row, ...]}"""
    out = {}
    for label, nums_txt, prize_txt in entries:
        gid, when = game_from_label(label)
        if not gid:
            continue
        nums = [int(x) for x in re.findall(r"\d+", nums_txt or "")]
        if not valid(gid, nums):
            print(f"  skipped {label} {date_iso}: unexpected numbers {nums_txt!r}")
            continue
        p = re.sub(r"[^\d.]", "", prize_txt or "")
        prize = float(p) if p else None
        row = [date_iso, nums, prize, None]
        if gid in ("3d", "2d"):
            if not when:
                continue
            row.append(when)
        out.setdefault(gid, []).append(row)
    return out


def parse_page(text):
    block = text.split('lotto_result-container', 1)
    if len(block) < 2:
        return []
    block = block[1].split('lotto-date-result', 1)[0]
    entries = []
    for m in re.finditer(r'data-type="([^"]+)".*?</a>\s*<p>(.*?)</p>(?:\s*<p class="--jackpot">(.*?)</p>)?', block, re.S):
        entries.append([html.unescape(m.group(1)), html.unescape(m.group(2)), html.unescape(m.group(3) or "")])
    return entries


def fetch_day(day):
    slug = f"{MONTHS[day.month - 1]}-{day.day}-{day.year}"
    url = f"https://www.gmanetwork.com/news/lotto/results-{slug}/"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # network problem
        print(f"  {url}: {e}")
        return 0, ""


# ---------------------------------------------------------------- merging
def row_key(gid, row):
    return (row[0], row[4] if gid in ("3d", "2d") and len(row) > 4 else "")


def sort_rows(gid, rows):
    rows.sort(key=lambda r: (r[0], TIME_ORDER.get(r[4], 9) if len(r) > 4 else 0))


def merge(data, new):
    added = 0
    for gid, rows in new.items():
        cur = data.setdefault(gid, [])
        index = {row_key(gid, r): i for i, r in enumerate(cur)}
        for r in rows:
            k = row_key(gid, r)
            if k in index:
                old = cur[index[k]]
                if old[1] != r[1]:
                    print(f"  corrected {gid} {k}: {old[1]} -> {r[1]}")
                    old[1] = r[1]
                if r[2] is not None:
                    old[2] = r[2]
            else:
                cur.append(r)
                index[k] = len(cur) - 1
                added += 1
        sort_rows(gid, cur)
    return added


def fetch_recent(days=DAYS):
    today = dt.datetime.now(MANILA).date()
    found, ok_pages, errors = {}, 0, []
    for i in range(days, -1, -1):
        day = today - dt.timedelta(days=i)
        status, text = fetch_day(day)
        if status == 200:
            entries = parse_page(text)
            if entries:
                ok_pages += 1
                for gid, rows in to_rows(day.isoformat(), entries).items():
                    found.setdefault(gid, []).extend(rows)
            else:
                errors.append(f"{day}: page found but no results could be read")
        elif status == 404:
            pass  # not published yet
        else:
            errors.append(f"{day}: HTTP {status}")
        time.sleep(2)
    return found, ok_pages, errors


# ---------------------------------------------------------------- page building
def fmt_int(n):
    return f"{int(round(n)):,}"


def peso(x):
    return "—" if x is None else f"₱{x:,.2f}"


def long_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def short_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}, {d.year}"


def table_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def combos(g):
    if g["kind"] == "pick":
        return math.comb(g["N"], 6)
    if g["kind"] == "digits":
        return 10 ** g["k"]
    return 31 * 31


def show(g, x):
    return str(x) if g["kind"] == "digits" else f"{x:02d}"


def balls(g, nums, small=False):
    cls = "ball sm" if small else "ball"
    return "".join(f'<span class="{cls}">{show(g, x)}</span>' for x in nums)


def latest_by_game(data):
    """For each game, the most recent day's draws (one row, or up to 3 for 3D/2D)."""
    out = {}
    for g in GAMES:
        rows = data.get(g["id"]) or []
        if not rows:
            continue
        last_day = rows[-1][0]
        out[g["id"]] = [r for r in rows if r[0] == last_day]
    return out


def latest_card(g, rows, link=True):
    day = rows[-1][0]
    if g["id"] in ("3d", "2d"):
        body = "".join(f'<div class="draw"><span class="when">{html.escape(r[4] if len(r) > 4 else "")}</span><div class="balls">{balls(g, r[1], True)}</div></div>' for r in rows)
    else:
        body = f'<div class="balls">{balls(g, rows[-1][1], True)}</div>'
        if rows[-1][2] is not None:
            label = "Jackpot" if g["kind"] == "pick" else "First prize"
            body += f'<span class="odds">{label}: <b>{peso(rows[-1][2])}</b></span>'
    title = f'<a href="/{g["slug"]}/">{g["name"]}</a>' if link else g["name"]
    return (f'<div class="lucky-card result-card"><h3>{title}</h3>'
            f'<span class="odds">{short_date(day)}</span>{body}</div>')


def site_css():
    css = re.search(r"<style>(.*?)</style>", (SITE / "index.html").read_text(encoding="utf-8"), re.S).group(1)
    # extra rules for generated pages and the results block (kept idempotent)
    return css


EXTRA_CSS = """
/* generated: results */
.result-card{align-content:start}
.result-card h3 a{color:var(--ink);text-decoration:none}.result-card h3 a:hover{color:var(--accent)}
.ball.sm{width:34px;height:34px;font-size:.85rem}
.draw{display:flex;align-items:center;gap:10px}.draw .when{font:600 .75rem var(--mono);color:var(--muted);min-width:42px}
.updated{font:500 .8rem var(--mono);color:var(--muted)}
.gamelinks{display:flex;flex-wrap:wrap;gap:8px}.gamelinks a{font:600 .85rem var(--mono);color:var(--accent);border:1px solid var(--line);background:var(--paper);padding:6px 11px;border-radius:999px;text-decoration:none}
.gamelinks a:hover{border-color:var(--accent)}
"""

NAV = ('<nav><a href="/#results">Results today</a><a href="/#odds">Odds</a><a href="/#value">Expected value</a>'
       '<a href="/#hot">Hot numbers?</a><a href="/how-pcso-odds-work/">Guide</a></nav>')

FOOTER = """  <footer>
    <p><strong>Play responsibly.</strong> The lottery is entertainment, not an investment. No method, software or past result can raise your chance of winning. Never play money you need for rent, food or school.</p>
    <p>This site is independent and not affiliated with the Philippine Charity Sweepstakes Office (PCSO). Results are collected automatically from published sources and may contain errors: always check pcso.gov.ph before claiming a prize.</p>
    <p class="footlinks"><a href="/">Home</a> · __GAMELINKS__ · <a href="/how-pcso-odds-work/">How PCSO odds work</a> · <a href="/about.html">About</a> · <a href="/privacy.html">Privacy Policy</a></p>
  </footer>"""


def footer():
    links = " · ".join(f'<a href="/{g["slug"]}/">{g["name"]}</a>' for g in GAMES)
    return FOOTER.replace("__GAMELINKS__", links)


def shell(title, desc, canonical, body, jsonld=""):
    css = site_css()
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PH Lotto Odds">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:url" content="{canonical}">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22%3E%3Ccircle cx=%2216%22 cy=%2216%22 r=%2214%22 fill=%22%23F2B33D%22/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>{css}</style>{jsonld}
</head>
<body>
<div class="wrap">
  <header class="top">
    <a class="brand" href="/"><span class="ballmini"></span>PH Lotto Odds</a>
    {NAV}
  </header>
{body}
{footer()}
</div>
</body>
</html>
"""


def pick_tier(N, k):
    return math.comb(6, k) * math.comb(N - 6, 6 - k) / math.comb(N, 6)


def odds_table(g):
    n = combos(g)
    if g["kind"] == "pick":
        rows = [("All 6 numbers (jackpot)", n)] + [(f"{k} numbers", 1 / pick_tier(g["N"], k)) for k in (5, 4, 3)]
        anyp = sum(pick_tier(g["N"], k) for k in (3, 4, 5, 6))
        rows.append(("Any prize", 1 / anyp))
    elif g["kind"] == "digits":
        rows = [(f"All {g['k']} digits in exact order", n)]
    else:
        rows = [("Both numbers in exact order", n)]
    body = "".join(f'<tr><td>{a}</td><td class="r num">1 in {fmt_int(b)}</td></tr>' for a, b in rows)
    return f'<table><thead><tr><th>Match</th><th class="r">Odds per ticket</th></tr></thead><tbody>{body}</tbody></table>'


def game_page(g, data, updated):
    rows = data.get(g["id"]) or []
    latest = latest_by_game(data).get(g["id"])
    multi = g["id"] in ("3d", "2d")
    recent = list(reversed(rows[-(60 if multi else 30):]))
    trs = "".join(
        f'<tr><td class="num">{table_date(r[0])}</td>' + (f'<td class="num">{html.escape(r[4] if len(r) > 4 else "")}</td>' if multi else "")
        + f'<td class="num">{" - ".join(show(g, x) for x in r[1])}</td><td class="r num">{peso(r[2])}</td></tr>'
        for r in recent)
    head = '<tr><th>Date</th>' + ('<th>Draw</th>' if multi else '') + '<th>Winning numbers</th><th class="r">' + ('Jackpot' if g["kind"] == "pick" else 'Prize') + '</th></tr>'
    n = combos(g)
    yrs = n / ((21 if multi else 3) * 52)
    wait = f"{fmt_int(yrs)} years" if yrs >= 1 else f"{fmt_int(yrs * 52)} weeks"
    order_note = ("The order of the numbers does not matter: you win if your six numbers match the six numbers drawn."
                  if g["kind"] == "pick" else
                  "Order matters: your numbers must match the draw in the exact order. Some outlets also sell “rambolito” bets that win in any order, with a smaller prize.")
    title = f"{g['name']} Result Today ({g['alt']}) · PCSO Results & Odds"
    if latest:
        last = latest[-1]
        desc = (f"{g['name']} result for {long_date(last[0])}: {' '.join(show(g, x) for x in last[1])}. "
                f"Latest PCSO {g['alt']} results, past winning numbers and the real odds of winning.")
    else:
        desc = f"Latest PCSO {g['name']} results, past winning numbers and the real odds of winning."
    latest_html = latest_card(g, latest, link=False) if latest else "<p>No results yet.</p>"
    body = f"""
  <section class="hero" aria-labelledby="h1">
    <p class="eyebrow">PCSO {html.escape(g['alt'])} · drawn {g['days']}</p>
    <h1 id="h1">{g['name']} result today</h1>
    <p class="updated">Updated automatically · last check {updated}</p>
    <div class="lucky-grid">{latest_html}</div>
  </section>

  <div class="adslot">Advertisement</div>

  <section aria-labelledby="h-recent">
    <h2 id="h-recent">Recent {g['name']} results</h2>
    <div class="card"><div class="tablewrap"><table><thead>{head}</thead><tbody>{trs}</tbody></table></div>
    <p class="note">The full archive since 2016 is on the <a href="/#archive">home page</a>.</p></div>
  </section>

  <section aria-labelledby="h-odds">
    <h2 id="h-odds">What are the odds of winning {g['name']}?</h2>
    <p class="muted">{order_note} A ticket costs ₱{g['price']}.</p>
    <div class="card"><div class="tablewrap">{odds_table(g)}</div></div>
    <div class="facts">
      <div class="fact"><b>1 in {fmt_int(n)}</b><span class="muted">Chance that one ticket wins the top prize.</span></div>
      <div class="fact"><b>{wait}</b><span class="muted">Average wait for a top prize buying one ticket for every draw.</span></div>
      <div class="fact"><b>{peso(n * g['price'])}</b><span class="muted">Cost of buying every possible combination.</span></div>
    </div>
    <p>Want to know what a ticket is really worth? Try the <a href="/#value">expected value calculator</a>, or read <a href="/how-pcso-odds-work/">how PCSO odds work</a>.</p>
  </section>

  <section aria-labelledby="h-other">
    <h2 id="h-other">Other PCSO games</h2>
    <div class="gamelinks">{''.join(f'<a href="/{o["slug"]}/">{o["name"]}</a>' for o in GAMES if o is not g)}</div>
  </section>
"""
    return shell(title, desc, f"{DOMAIN}/{g['slug']}/", body)


GUIDE_BODY = (Path(__file__).resolve().parent / "guide_body.html")


def guide_page():
    body = GUIDE_BODY.read_text(encoding="utf-8")
    t = "How PCSO Lotto Odds Work: A Plain-Math Guide (Tsansa sa Lotto)"
    d = ("How the odds of PCSO games are calculated, why order matters in Swertres and EZ2, what a ticket is worth on average, "
         "the 20% tax on prizes and why hot numbers do not exist.")
    return shell(t, d, f"{DOMAIN}/how-pcso-odds-work/", body)


def results_block(data, updated):
    latest = latest_by_game(data)
    cards = "".join(latest_card(g, latest[g["id"]]) for g in GAMES if g["id"] in latest)
    return f"""<!--RESULTS:START-->
  <section id="results" aria-labelledby="h-results">
    <p class="eyebrow">PCSO lotto result today</p>
    <h2 id="h-results">Latest PCSO results</h2>
    <p class="updated">Updated automatically after the 2 PM, 5 PM and 9 PM draws · last check {updated}</p>
    <div class="lucky-grid">{cards}</div>
  </section>
<!--RESULTS:END-->"""


def build(data):
    updated = dt.datetime.now(MANILA).strftime("%b %d, %Y, %I:%M %p").replace(" 0", " ") + " Manila time"
    idx_path = SITE / "index.html"
    idx = idx_path.read_text(encoding="utf-8")
    block = results_block(data, updated)
    if "<!--RESULTS:START-->" in idx:
        idx = re.sub(r"<!--RESULTS:START-->.*?<!--RESULTS:END-->", lambda m: block, idx, flags=re.S)
    else:
        idx = idx.replace('  <section id="lucky"', block + '\n\n  <section id="lucky"', 1)
    if "/* generated: results */" not in idx:
        idx = idx.replace("</style>", EXTRA_CSS + "</style>", 1)
    idx = re.sub(r"<nav>.*?</nav>", lambda m: NAV, idx, count=1, flags=re.S)
    idx = re.sub(r"  <footer>.*?</footer>", lambda m: footer(), idx, count=1, flags=re.S)
    idx_path.write_text(idx, encoding="utf-8")

    for g in GAMES:
        d = SITE / g["slug"]
        d.mkdir(exist_ok=True)
        (d / "index.html").write_text(game_page(g, data, updated), encoding="utf-8")
    gd = SITE / "how-pcso-odds-work"
    gd.mkdir(exist_ok=True)
    (gd / "index.html").write_text(guide_page(), encoding="utf-8")

    today = dt.datetime.now(MANILA).date().isoformat()
    urls = ["/"] + [f"/{g['slug']}/" for g in GAMES] + ["/how-pcso-odds-work/", "/about.html", "/privacy.html"]
    sm = "".join(f"  <url><loc>{DOMAIN}{u}</loc><lastmod>{today}</lastmod></url>\n" for u in urls)
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + sm + "</urlset>\n",
        encoding="utf-8")


def save(data):
    DATA.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def main(argv):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    if "--from-file" in argv:
        saved = json.loads(Path(argv[argv.index("--from-file") + 1]).read_text(encoding="utf-8"))
        new = {}
        for slug, entries in saved.items():
            mon, day, year = slug.split("-")
            iso = dt.date(int(year), MONTHS.index(mon) + 1, int(day)).isoformat()
            for gid, rows in to_rows(iso, entries).items():
                new.setdefault(gid, []).extend(rows)
        print(f"added {merge(data, new)} draws from file")
        save(data)
    elif "--build-only" not in argv:
        new, ok_pages, errors = fetch_recent()
        for e in errors:
            print("  problem:", e)
        added = merge(data, new)
        print(f"read {ok_pages} result pages, added {added} new draws")
        save(data)
        if ok_pages == 0:
            build(data)
            sys.exit("No result page could be read: the source may have changed or blocked the request.")
    build(data)


if __name__ == "__main__":
    main(sys.argv[1:])
