#!/usr/bin/env python3
"""memepoetry: a sentence -> URL database for found poetry.

Stores single-sentence "meme poetry" lines (the one-line-over-an-image style of
accounts like @wannakissyourscars) together with every URL where each line was
seen. Built on SQLite + FTS5 so the corpus is a single portable file that
supports full-text search for composing found poetry.

Data model:
  sentences  one row per distinct line (after normalization)
  sources    one row per (sentence, URL) sighting; a line can have many URLs
  sentences_fts  FTS5 index over sentence text, kept in sync by triggers

Usage examples:
  ./memepoetry.py init
  ./memepoetry.py add --text "i want to kiss your scars" \
      --url "https://www.instagram.com/p/XXXX/" --account wannakissyourscars
  ./memepoetry.py import seed.jsonl
  ./memepoetry.py ingest-export instagram-export.zip
  ./memepoetry.py canon "https://instagram.com/reels/XXXX/?igsh=abc"
  ./memepoetry.py fetch "https://www.instagram.com/p/XXXX/"
  ./memepoetry.py search "scars"
  ./memepoetry.py compose --query "night OR stars" --lines 5
  ./memepoetry.py export > backup.jsonl
"""

import argparse
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "memepoetry.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sentences (
    id        INTEGER PRIMARY KEY,
    text      TEXT NOT NULL,
    text_norm TEXT NOT NULL UNIQUE,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    sentence_id INTEGER NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    platform    TEXT,
    account     TEXT,
    post_type   TEXT,
    notes       TEXT,
    captured_at TEXT NOT NULL,
    UNIQUE (sentence_id, url)
);

CREATE VIRTUAL TABLE IF NOT EXISTS sentences_fts USING fts5(
    text, content='sentences', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS sentences_ai AFTER INSERT ON sentences BEGIN
    INSERT INTO sentences_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS sentences_ad AFTER DELETE ON sentences BEGIN
    INSERT INTO sentences_fts(sentences_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS sentences_au AFTER UPDATE ON sentences BEGIN
    INSERT INTO sentences_fts(sentences_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
    INSERT INTO sentences_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

# Characters commonly wrapped around meme-poetry lines that carry no identity:
# quotes, brackets, dashes, ellipses, terminal punctuation.
_STRIP_CHARS = "\"'‘’“”`([{<—–-….!?,;: >}])"


def normalize(text: str) -> str:
    """Canonical form used for deduplication (not for display)."""
    text = unicodedata.normalize("NFKC", text)
    # Drop emoji / symbols so "the moon 🌙" and "the moon" collide.
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "S")
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(_STRIP_CHARS + " ")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def guess_platform(url: str) -> str | None:
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    known = {
        "instagram.com": "instagram",
        "tiktok.com": "tiktok",
        "tumblr.com": "tumblr",
        "pinterest.com": "pinterest",
        "x.com": "x",
        "twitter.com": "x",
    }
    for domain, name in known.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


# Post paths, optionally prefixed with a username segment ("/user/p/CODE/").
# /reels/ and /tv/ are historical aliases that redirect to /reel/.
_IG_POST_PATH = re.compile(r"^/(?:[^/]+/)?(p|reel|reels|tv)/([A-Za-z0-9_-]+)/?")


def _instagram_path(url: str) -> str | None:
    """Return the URL's path if it is an Instagram link, else None."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().rsplit(":", 1)[0]
    host = re.sub(r"^(www|m)\.", "", host)
    return parts.path if host in ("instagram.com", "instagr.am") else None


def canonicalize_url(url: str, resolve: bool = False) -> str:
    """Normalize Instagram links to the stable /p/ or /reel/ shortcode form.

    Pasted share links carry tracking query strings, mobile or bare hosts,
    a leading username segment, or the /reels/ and /tv/ aliases; all of
    those collapse to https://www.instagram.com/{p,reel}/<shortcode>/.
    Opaque links (e.g. /share/...) hide the shortcode behind a redirect and
    are only rewritten when resolve=True permits a network round trip.
    Non-Instagram URLs pass through unchanged.
    """
    url = url.strip()
    path = _instagram_path(url)
    if path is None:
        return url
    m = _IG_POST_PATH.match(path)
    # "/share/p/<token>" carries an opaque share token, not the shortcode.
    if m and path.split("/")[1] != "share":
        kind = "p" if m.group(1) == "p" else "reel"
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    if resolve:
        resolved = _resolve_instagram_url(url)
        if resolved:
            return canonicalize_url(resolved)
    return url


def fetch_oembed(url: str) -> dict | None:
    """Fetch post metadata via Instagram's official oEmbed API.

    Returns {"url", "account", "caption", "post_type"} (values may be None
    beyond "url") or None when the post can't be fetched (private, deleted,
    rate-limited, or no network). Uses INSTAGRAM_OEMBED_TOKEN (a Facebook
    app token) when set; otherwise tries tokenless access, which Meta
    re-enabled for public posts in mid-2026.
    """
    url = canonicalize_url(url)
    token = os.environ.get("INSTAGRAM_OEMBED_TOKEN")
    params = {"url": url, "omitscript": "true"}
    if token:
        params["access_token"] = token
    api = ("https://graph.facebook.com/v23.0/instagram_oembed?"
           + urllib.parse.urlencode(params))
    try:
        req = urllib.request.Request(
            api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (OSError, ValueError) as e:
        print(f"oembed fetch failed for {url} ({e})", file=sys.stderr)
        return None
    embed = data.get("html", "")
    m = re.search(r'data-instgrm-permalink="([^"?#]+)', embed)
    permalink = canonicalize_url(m.group(1)) if m else url
    # The embed blockquote carries the caption as the first <p>'s link text.
    m = re.search(r"<p[^>]*>\s*<a[^>]*>(.*?)</a>", embed, re.S)
    caption = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))) if m else None
    return {
        "url": permalink,
        "account": data.get("author_name"),
        "caption": caption.strip() if caption else None,
        "post_type": "reel" if "/reel/" in permalink else None,
    }


def caption_to_line(caption: str | None) -> str | None:
    """Pick the poem line out of a caption: the first line that still has
    words once hashtags and @credits are removed."""
    for line in (caption or "").splitlines():
        line = re.sub(r"[#@]\S+", "", line).strip()
        if any(ch.isalpha() for ch in line):
            return line
    return None


def _resolve_instagram_url(url: str) -> str | None:
    """Resolve an opaque Instagram link to its permalink over the network:
    via oEmbed when it accepts the link, else by following the redirect."""
    rec = fetch_oembed(url)
    if rec and rec["url"] != url:
        return rec["url"]
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url
    except OSError as e:
        print(f"could not resolve {url} ({e})", file=sys.stderr)
        return None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def add_entry(conn, text, url, platform=None, account=None, post_type=None,
              notes=None, resolve=False):
    """Insert a sighting; dedupes on normalized text and on (sentence, url).

    URLs are canonicalized first so the same post pasted two ways dedupes.
    Returns (sentence_id, created_sentence, created_source).
    """
    url = canonicalize_url(url, resolve=resolve)
    norm = normalize(text)
    if not norm:
        raise ValueError(f"sentence is empty after normalization: {text!r}")
    row = conn.execute(
        "SELECT id FROM sentences WHERE text_norm = ?", (norm,)).fetchone()
    created_sentence = row is None
    if created_sentence:
        cur = conn.execute(
            "INSERT INTO sentences (text, text_norm, added_at) VALUES (?,?,?)",
            (text.strip(), norm, now()))
        sentence_id = cur.lastrowid
    else:
        sentence_id = row["id"]
    cur = conn.execute(
        """INSERT OR IGNORE INTO sources
           (sentence_id, url, platform, account, post_type, notes, captured_at)
           VALUES (?,?,?,?,?,?,?)""",
        (sentence_id, url, platform or guess_platform(url), account,
         post_type, notes, now()))
    return sentence_id, created_sentence, cur.rowcount > 0


def sentence_with_sources(conn, sentence_id):
    s = conn.execute(
        "SELECT * FROM sentences WHERE id = ?", (sentence_id,)).fetchone()
    urls = conn.execute(
        "SELECT * FROM sources WHERE sentence_id = ? ORDER BY captured_at",
        (sentence_id,)).fetchall()
    return s, urls


def cmd_init(args, conn):
    print(f"initialized {args.db}")


def cmd_add(args, conn):
    text, account, post_type = args.text, args.account, args.post_type
    if text is None:
        rec = fetch_oembed(args.url) or {}
        text = caption_to_line(rec.get("caption"))
        if not text:
            sys.exit("could not get a line via oEmbed; pass --text")
        account = account or rec.get("account")
        post_type = post_type or rec.get("post_type")
        print(f"caption line: {text}", file=sys.stderr)
    sid, new_s, new_u = add_entry(
        conn, text, args.url, platform=args.platform,
        account=account, post_type=post_type, notes=args.notes,
        resolve=args.resolve)
    conn.commit()
    state = ("new sentence" if new_s else
             "known sentence, new URL" if new_u else
             "duplicate (sentence and URL already recorded)")
    print(f"#{sid}: {state}")


def cmd_import(args, conn):
    stream = open(args.file) if args.file != "-" else sys.stdin
    added_s = added_u = skipped = 0
    with stream:
        for lineno, line in enumerate(stream, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rec = json.loads(line)
                _, new_s, new_u = add_entry(
                    conn, rec["text"], rec["url"],
                    platform=rec.get("platform"), account=rec.get("account"),
                    post_type=rec.get("post_type"), notes=rec.get("notes"),
                    resolve=args.resolve)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                print(f"line {lineno}: skipped ({e})", file=sys.stderr)
                skipped += 1
                continue
            added_s += new_s
            added_u += new_u
    conn.commit()
    print(f"imported: {added_s} new sentences, {added_u} new URLs, "
          f"{skipped} lines skipped")


def _walk_hrefs(node, account=None):
    """Yield (href, account) for every Instagram link in a Meta export blob.

    Export entries nest the link under the entry's "title" (the account),
    e.g. {"title": "acct", "string_map_data": {"Saved on": {"href": ...}}},
    but the exact shape varies by file and era, so walk generically and
    inherit the nearest enclosing title.
    """
    if isinstance(node, dict):
        account = node.get("title") or account
        href = node.get("href")
        if isinstance(href, str) and _instagram_path(href) is not None:
            yield href, account
        for v in node.values():
            yield from _walk_hrefs(v, account)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_hrefs(v, account)


def _export_sightings(path: Path, filenames):
    """Yield (url, account) from a Meta "Download your information" export:
    the zip, an extracted directory, or one of its JSON files directly."""
    blobs = []
    if path.is_dir():
        for name in filenames:
            blobs += [f.read_text() for f in path.rglob(name)]
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            blobs += [z.read(n).decode() for n in z.namelist()
                      if n.endswith(tuple(filenames))]
    else:
        blobs.append(path.read_text())
    if not blobs:
        raise FileNotFoundError(
            f"no {' or '.join(filenames)} found under {path}")
    for blob in blobs:
        yield from _walk_hrefs(json.loads(blob))


def cmd_ingest(args, conn):
    """Ingest saved posts from a Meta export, enriching each via oEmbed."""
    filenames = ["saved_posts.json"] + (["liked_posts.json"] if args.liked
                                        else [])
    seen, pending = set(), []
    if Path(args.pending).exists():  # don't refetch posts already queued
        for line in Path(args.pending).read_text().splitlines():
            try:
                seen.add(json.loads(line).get("url"))
            except json.JSONDecodeError:
                pass
    fetched = added = 0
    for url, account in _export_sightings(args.path, filenames):
        url = canonicalize_url(url)
        if url in seen:
            continue
        seen.add(url)
        if conn.execute("SELECT 1 FROM sources WHERE url = ?",
                        (url,)).fetchone():
            continue
        if args.limit is not None and fetched >= args.limit:
            print(f"stopping at --limit {args.limit}; rerun to continue")
            break
        if args.dry_run:
            print(f"would fetch {url}" + (f" (@{account})" if account else ""))
            continue
        if fetched:
            time.sleep(args.sleep)
        fetched += 1
        rec = fetch_oembed(url) or {}
        text = caption_to_line(rec.get("caption"))
        if not text:
            reason = ("caption has no usable line" if rec
                      else "oembed fetch failed")
            pending.append({"text": "", "url": rec.get("url") or url,
                            "account": rec.get("account") or account,
                            "notes": f"pending: {reason}"})
            continue
        _, new_s, _ = add_entry(
            conn, text, rec["url"], account=rec.get("account") or account,
            post_type=rec.get("post_type"),
            notes="text from caption via oEmbed (unverified)")
        added += new_s
        print(f'+ {text}  [{rec["url"]}]')
    conn.commit()
    print(f"{fetched} posts fetched: {added} new sentences, "
          f"{len(pending)} left for manual entry")
    if pending:
        with open(args.pending, "a") as f:
            for rec in pending:
                f.write(json.dumps(
                    {k: v for k, v in rec.items() if v is not None},
                    ensure_ascii=False) + "\n")
        print(f"appended {len(pending)} rows to {args.pending}; fill in "
              f'"text" and run: ./memepoetry.py import {args.pending}')


def cmd_export(args, conn):
    rows = conn.execute(
        """SELECT s.text, src.url, src.platform, src.account, src.post_type,
                  src.notes
           FROM sources src JOIN sentences s ON s.id = src.sentence_id
           ORDER BY s.id, src.id""")
    for r in rows:
        rec = {k: r[k] for k in r.keys() if r[k] is not None}
        print(json.dumps(rec, ensure_ascii=False))


def _print_hits(conn, rows):
    for r in rows:
        _, urls = sentence_with_sources(conn, r["id"])
        print(f'#{r["id"]}  {r["text"]}')
        for u in urls:
            who = f' (@{u["account"]})' if u["account"] else ""
            print(f'       {u["url"]}{who}')


def cmd_search(args, conn):
    rows = conn.execute(
        """SELECT s.id, s.text FROM sentences_fts f
           JOIN sentences s ON s.id = f.rowid
           WHERE sentences_fts MATCH ? ORDER BY rank LIMIT ?""",
        (args.query, args.limit)).fetchall()
    if not rows:
        print("no matches")
        return
    _print_hits(conn, rows)


def cmd_lookup(args, conn):
    if args.url:
        rows = conn.execute(
            """SELECT DISTINCT s.id, s.text FROM sources src
               JOIN sentences s ON s.id = src.sentence_id
               WHERE src.url = ?""", (canonicalize_url(args.url),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text FROM sentences WHERE text_norm = ?",
            (normalize(args.text),)).fetchall()
    if not rows:
        print("not found")
        return
    _print_hits(conn, rows)


def cmd_compose(args, conn):
    """Draft a found poem: N lines drawn from the corpus, sources footnoted."""
    if args.query:
        rows = conn.execute(
            """SELECT s.id, s.text FROM sentences_fts f
               JOIN sentences s ON s.id = f.rowid
               WHERE sentences_fts MATCH ? ORDER BY rank LIMIT ?""",
            (args.query, args.lines * 4)).fetchall()
    else:
        rows = conn.execute("SELECT id, text FROM sentences").fetchall()
    if not rows:
        print("corpus is empty (or no matches); add sentences first")
        return
    picked = random.sample(rows, min(args.lines, len(rows)))
    print()
    for i, r in enumerate(picked, 1):
        print(f"  {r['text']}   [{i}]")
    print()
    for i, r in enumerate(picked, 1):
        _, urls = sentence_with_sources(conn, r["id"])
        print(f"[{i}] " + (urls[0]["url"] if urls else "(no source recorded)"))


def cmd_stats(args, conn):
    n_s = conn.execute("SELECT COUNT(*) c FROM sentences").fetchone()["c"]
    n_u = conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    by_acct = conn.execute(
        """SELECT account, COUNT(*) c FROM sources
           WHERE account IS NOT NULL GROUP BY account ORDER BY c DESC
           LIMIT 10""").fetchall()
    print(f"{n_s} sentences, {n_u} source URLs")
    for r in by_acct:
        print(f'  @{r["account"]}: {r["c"]}')


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="memepoetry", description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help=f"database file (default: {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database file")

    sp = sub.add_parser("add", help="record one sentence sighting")
    sp.add_argument("--text", help="the line (default: post caption via oEmbed)")
    sp.add_argument("--url", required=True)
    sp.add_argument("--platform")
    sp.add_argument("--account", help="handle without the @")
    sp.add_argument("--post-type", choices=["image", "reel", "carousel"])
    sp.add_argument("--notes")
    sp.add_argument("--resolve", action="store_true",
                    help="resolve opaque Instagram share links over the network")

    sp = sub.add_parser("import", help="bulk import JSONL (text,url,... per line)")
    sp.add_argument("file", help="path, or - for stdin")
    sp.add_argument("--resolve", action="store_true",
                    help="resolve opaque Instagram share links over the network")

    sp = sub.add_parser("canon", help="print a URL's canonical form")
    sp.add_argument("url")
    sp.add_argument("--resolve", action="store_true",
                    help="resolve opaque Instagram share links over the network")

    sp = sub.add_parser("fetch", help="print a post's oEmbed metadata as JSON")
    sp.add_argument("url")

    sp = sub.add_parser(
        "ingest-export",
        help="ingest saved posts from a Meta 'Download your information' "
             "export (zip, extracted directory, or saved_posts.json)")
    sp.add_argument("path", type=Path)
    sp.add_argument("--liked", action="store_true",
                    help="also ingest liked_posts.json")
    sp.add_argument("--pending", default="pending.jsonl",
                    help="JSONL file for posts needing manual text "
                         "(default: %(default)s)")
    sp.add_argument("--limit", type=int,
                    help="max posts to fetch this run")
    sp.add_argument("--sleep", type=float, default=1.0,
                    help="seconds between oEmbed calls (default: 1)")
    sp.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched, no network or writes")

    sub.add_parser("export", help="dump the corpus as JSONL to stdout")

    sp = sub.add_parser("search", help="full-text search (FTS5 query syntax)")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("lookup", help="exact lookup by URL or sentence")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--text")

    sp = sub.add_parser("compose", help="draft a found poem from the corpus")
    sp.add_argument("--query", help="restrict lines to an FTS5 match")
    sp.add_argument("--lines", type=int, default=5)
    sp.add_argument("--seed", type=int, help="random seed for reproducibility")

    sub.add_parser("stats", help="corpus summary")

    args = p.parse_args(argv)
    if args.cmd == "canon":  # needs no database
        print(canonicalize_url(args.url, resolve=args.resolve))
        return
    if args.cmd == "fetch":  # likewise
        rec = fetch_oembed(args.url)
        if rec is None:
            sys.exit(1)
        rec["text"] = caption_to_line(rec.get("caption"))
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return
    if getattr(args, "seed", None) is not None:
        random.seed(args.seed)
    conn = connect(args.db)
    try:
        {"init": cmd_init, "add": cmd_add, "import": cmd_import,
         "ingest-export": cmd_ingest, "export": cmd_export,
         "search": cmd_search, "lookup": cmd_lookup,
         "compose": cmd_compose, "stats": cmd_stats}[args.cmd](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
