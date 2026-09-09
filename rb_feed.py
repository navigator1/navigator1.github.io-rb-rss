#!/usr/bin/env python3
"""
Bygg och underhåll en komplett RSS-feed av Risky Business-arkivet.

Deras arkivsida listar hela katalogen sedan 2007 på en enda sida. Skriptet
hämtar den, plockar MP3-länken från varje avsnittssida och skriver ut en
feed sorterad äldst först.

Kör det igen när som helst för att uppdatera: allt som redan ligger i
cachen hoppas över, så en uppdatering kostar bara de nya avsnitten.

  python rb_feed.py                          # allt, till rb.xml
  python rb_feed.py --chunk 200              # rb-001.xml, rb-002.xml, ...
  python rb_feed.py --count 200              # bara de 200 äldsta
  python rb_feed.py --retry-missing          # nytt försök på de som saknar ljud

Andra feeds på samma sajt via --archive, t.ex.
  https://risky.biz/netcasts/risky-business-news/
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

ARCHIVE = "https://risky.biz/netcasts/risky-business/"
CACHE = "rb_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-archive-feed/1.0)"}

# Girig match: podtrac-URL:er ser ut som .../redirect.mp3/media3.risky.biz/RB851.mp3
# och en icke-girig regex skulle stanna vid den första .mp3-ändelsen.
AUDIO_RE = re.compile(r'https?://[^\s"\'<>]+\.mp3(?:\?[^\s"\'<>]*)?', re.I)

ENTRY_RE = re.compile(
    r'href="(?P<url>[^"]+)"[^>]*>(?P<title>[^<]{5,}?)</a>'
    r'.{0,300}?(?P<date>\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})',
    re.S,
)
DURATION_RE = re.compile(r'"duration"\s*:\s*"?(\d+)', re.I)

MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def get(url, timeout=60, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 401):
                raise
            last = e
        except Exception as e:
            last = e
        wait = 2 ** attempt
        print(f"    försök {attempt + 1} gav {last}, väntar {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise last


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {"episodes": {}}


def save_cache(cache):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def parse_archive(html, base="https://risky.biz"):
    """Plocka ut alla avsnitt ur arkivsidan: URL, titel och datum."""
    out, seen = [], set()
    for m in ENTRY_RE.finditer(html):
        url = m.group("url")
        if url.startswith("/"):
            url = base + url
        if "risky.biz" not in url:
            continue
        if url in seen or "/category/" in url or "/feeds/" in url:
            continue
        if url.rstrip("/").endswith("/netcasts/risky-business"):
            continue
        day, mon, year = m.group("date").split()
        if mon not in MONTHS:
            continue
        seen.add(url)
        out.append({
            "url": url,
            "title": re.sub(r"\s+", " ", m.group("title")).strip(),
            "date": datetime(int(year), MONTHS[mon], int(day),
                             tzinfo=timezone.utc).isoformat(),
        })
    out.sort(key=lambda e: e["date"])          # äldst först
    return out


def build_rss(episodes, part=None, of=None):
    title = "Risky Business (arkiv)"
    if of and of > 1:
        title += f" [{part}/{of}]"
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        "<link>https://risky.biz/</link>",
        "<description>Hela katalogen, äldsta avsnittet först.</description>",
        "<language>en</language>",
        f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>",
        "<itunes:author>Risky Business Media</itunes:author>",
        "<itunes:explicit>false</itunes:explicit>",
    ]
    for ep in episodes:
        pub = format_datetime(datetime.fromisoformat(ep["date"]))
        out += [
            "<item>",
            f"<title>{escape(ep['title'])}</title>",
            f"<link>{escape(ep['url'])}</link>",
            f'<guid isPermaLink="true">{escape(ep["url"])}</guid>',
            f"<pubDate>{pub}</pubDate>",
            f'<enclosure url="{escape(ep["audio"])}" length="0" type="audio/mpeg"/>',
        ]
        if ep.get("duration"):
            out.append(f"<itunes:duration>{ep['duration']}</itunes:duration>")
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int,
                   help="Bara de N äldsta (utelämna för allt)")
    p.add_argument("-o", "--output", default="rb.xml")
    p.add_argument("--chunk", type=int, help="Dela upp i N avsnitt per fil")
    p.add_argument("--delay", type=float, default=0.5)
    p.add_argument("--archive", default=ARCHIVE, help="Annan arkivsida")
    p.add_argument("--retry-missing", action="store_true",
                   help="Försök igen med avsnitt där ingen ljudfil hittades")
    p.add_argument("--newest-first", action="store_true")
    args = p.parse_args()

    cache = load_cache()
    known_before = sum(1 for v in cache["episodes"].values() if v.get("audio"))

    print("Hämtar arkivsidan...", file=sys.stderr)
    entries = parse_archive(get(args.archive))
    if not entries:
        sys.exit("Kunde inte tolka arkivsidan - strukturen kan ha ändrats.")
    print(f"{len(entries)} avsnitt i arkivet, "
          f"{entries[0]['date'][:10]} till {entries[-1]['date'][:10]}",
          file=sys.stderr)

    wanted = entries[:args.count] if args.count else entries

    todo = []
    for ep in wanted:
        c = cache["episodes"].get(ep["url"])
        if c and c.get("audio"):
            ep["audio"] = c["audio"]
            ep["duration"] = c.get("duration")
        elif c and not args.retry_missing:
            continue                       # känd miss, hoppa över
        else:
            todo.append(ep)

    if todo:
        print(f"{len(todo)} nya avsnitt att hämta "
              f"({len(wanted) - len(todo)} redan i cachen).", file=sys.stderr)
    else:
        print("Inga nya avsnitt - allt fanns i cachen.", file=sys.stderr)

    for i, ep in enumerate(todo, 1):
        try:
            html = get(ep["url"])
        except Exception as e:
            print(f"  hoppar över {ep['url']}: {e}", file=sys.stderr)
            continue
        m = AUDIO_RE.search(html)
        d = DURATION_RE.search(html)
        entry = {"audio": m.group(0) if m else None,
                 "duration": d.group(1) if d else None}
        cache["episodes"][ep["url"]] = entry
        if entry["audio"]:
            ep["audio"], ep["duration"] = entry["audio"], entry["duration"]
        if i % 20 == 0:
            save_cache(cache)
            print(f"  {i}/{len(todo)}", file=sys.stderr)
        time.sleep(args.delay)

    save_cache(cache)

    ready = [e for e in wanted if e.get("audio")]
    if not ready:
        sys.exit("Inga ljudlänkar hittades.")
    if args.newest_first:
        ready.reverse()

    base, ext = os.path.splitext(args.output)
    if args.chunk:
        parts = [ready[i:i + args.chunk] for i in range(0, len(ready), args.chunk)]
        for i, group in enumerate(parts, 1):
            name = f"{base}-{i:03d}{ext}"
            with open(name, "w", encoding="utf-8") as f:
                f.write(build_rss(group, i, len(parts)))
            print(f"  {name}: {len(group)} avsnitt", file=sys.stderr)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(build_rss(ready))
        print(f"Skrev {args.output}", file=sys.stderr)

    added = sum(1 for v in cache["episodes"].values() if v.get("audio")) - known_before
    missing = len(wanted) - len(ready)
    print(f"Klart: {len(ready)} avsnitt ({added} nya den här körningen).",
          file=sys.stderr)
    if missing:
        print(f"{missing} saknar ljudfil - kör med --retry-missing för nytt försök.",
              file=sys.stderr)
    print(f"Spann: {ready[0]['date'][:10]} till {ready[-1]['date'][:10]}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
