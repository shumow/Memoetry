# memoetry

A sentence → URL database for the "one poetic line over a picture/reel"
Instagram genre (e.g. @wannakissyourscars, @VictorianPoetry), plus a small
CLI for searching the corpus and drafting found poetry from it.

## What the trend is called

There is no single settled name; the genre sits at the intersection of a few
named things:

- **Instapoetry / micropoetry** — the umbrella terms for short poetry native
  to Instagram; a single aphoristic line is the micro end of it.
- **Web weaving** (Tumblr, 2019; revived on TikTok as **quote dumps**) — the
  curatorial practice of collecting poetic fragments over images. The
  single-image, single-sentence post is essentially a one-slide web weaving.
- **Hopecore / corecore** — the reel-editing aesthetic these accounts use:
  one emotionally loaded line over found footage with an ambient song.
- **Image macro** is the old, format-level term for any text-over-image meme;
  these accounts are the literary register of it.

If you need one label for the corpus, "micro instapoetry" or "one-line web
weaving" communicates it best.

## Data model

SQLite (single file, `data/memepoetry.sqlite3`) with FTS5 full-text search:

- `sentences` — one row per distinct line. Deduplication is by a normalized
  form (NFKC, casefolded, whitespace collapsed, surrounding punctuation and
  emoji stripped), so "The moon 🌙" and "the moon." are the same sentence.
- `sources` — one row per (sentence, URL) sighting. The same line circulating
  on many accounts is the normal case in this genre, and tracking *all* its
  URLs is the interesting part (provenance, spread, earliest sighting).
- `sentences_fts` — FTS5 index kept in sync by triggers.

## Usage

```sh
./memepoetry.py init
./memepoetry.py add --text "we were a museum of almosts" \
    --url "https://www.instagram.com/p/XXXX/" --account someaccount --post-type reel
./memepoetry.py import seed.example.jsonl     # bulk JSONL, or `-` for stdin
./memepoetry.py ingest-export instagram-export.zip   # Meta DYI saved posts
./memepoetry.py canon "https://instagram.com/reels/XXXX/?igsh=abc"
./memepoetry.py fetch "https://www.instagram.com/p/XXXX/"  # oEmbed metadata
./memepoetry.py search "moon OR stars"        # FTS5 query syntax
./memepoetry.py lookup --url "https://www.instagram.com/p/XXXX/"
./memepoetry.py compose --query "grief" --lines 5 --seed 42
./memepoetry.py export > backup.jsonl
./memepoetry.py stats
```

`compose` drafts a found poem: it samples lines (optionally restricted to an
FTS match) and prints each source URL as a footnote, so every published poem
can carry attribution.

JSONL import format, one object per line (`#` comments and blank lines
ignored): `{"text": ..., "url": ..., "account"?, "platform"?, "post_type"?,
"notes"?}`. Platform is inferred from the URL when omitted.

## On collecting the data

- **Curated entry through official channels is the intended path.**
  Instagram's terms prohibit automated scraping, and logged-out access is
  blocked anyway. Two workflows this supports:
  - As you browse, copy the line and the post's share URL
    (`https://www.instagram.com/p/<shortcode>/` or `/reel/<shortcode>/`)
    into `add`, or batch sightings in a JSONL file and `import` them.
  - **The saved-posts funnel**: tap *Save* on posts as you scroll, then
    periodically request Meta's "Download your information" export (scoped
    to Instagram → Saved is enough, JSON format) and run
    `ingest-export` on the zip. Each new URL is enriched via the official
    oEmbed API — account from `author_name`, the line from the caption
    (first line that survives stripping hashtags/@credits), tagged
    `notes: "text from caption via oEmbed (unverified)"` since the caption
    may differ from the image overlay. Posts whose caption yields no usable
    line (or that oEmbed can't serve) are appended to `pending.jsonl`;
    fill in their `"text"` by hand and `import` the file. Reruns are
    incremental: URLs already in the database or in `pending.jsonl` are
    skipped. `--liked` also ingests liked posts, `--limit`/`--sleep`
    keep API usage polite, `--dry-run` previews.
- `add --url <url>` without `--text` does the same single-post enrichment,
  and `fetch <url>` prints the raw oEmbed record. oEmbed works tokenless
  for public posts (again, since mid-2026); set `INSTAGRAM_OEMBED_TOKEN`
  (a Facebook app token) to use the token lane and its higher rate limits.
- Post URLs are stable canonical IDs — store the `/p/` or `/reel/` form, not
  a feed or story URL. Instagram links are canonicalized automatically on
  `add`/`import`/`lookup`: tracking query strings, mobile and bare hosts,
  username prefixes, and the `/reels/`/`/tv/` aliases all collapse to
  `https://www.instagram.com/{p,reel}/<shortcode>/`, so the same post pasted
  two ways dedupes to one sighting (`canon` previews the rewrite). Opaque
  `/share/...` links hide the shortcode behind a redirect; pass `--resolve`
  to look them up over the network — via the official oEmbed endpoint when
  `INSTAGRAM_OEMBED_TOKEN` is set (a Facebook app token with oEmbed Read),
  otherwise by following the redirect.
- Found poetry made from these lines is a derivative use of other people's
  words; the footnoted-URL output of `compose` exists so attribution travels
  with every draft.

The database file itself is gitignored; `export` to JSONL for anything you
want to version or share.
