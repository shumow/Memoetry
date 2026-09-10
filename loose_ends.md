# Loose ends

Leftovers from moving this project out of PRIMES-Conjectures (2026-09-08).

## GitHub cleanup (manual, one-time)

- [ ] Delete the empty typo-spelled repo `shumow/memeotry` if it still exists
      (nothing was ever pushed to it; this repo, `Memoetry`, is the real one).
      *Checked 2026-09-08: it still exists and is still empty (zero refs).
      Repo deletion needs the web UI (Settings → Danger Zone) or an
      admin-scoped token, so it stays manual.*
- [ ] Delete the branch `claude/meme-poetry-database-4pn6ff` in
      `shumow/PRIMES-Conjectures`. The meme-poetry work was force-pushed away
      and the branch now points at the same commit as `main`, so it is inert.
      *Checked 2026-09-08: confirmed inert (same SHA as `main`, 37cc491), but
      the branch-delete push is blocked by the remote session's egress policy
      (HTTP 403), so it also stays manual: delete it from the branches page or
      run `git push origin --delete claude/meme-poetry-database-4pn6ff`.*
- [ ] Delete the branch `claude/loose-ends-tasks-5uzjlk` in this repo. Its
      work was fast-forwarded into `main` (a8283b4) on 2026-09-08, so it is
      inert; branch-delete pushes are blocked from remote sessions (same
      HTTP 403 as above), so delete it from the branches page or run
      `git push origin --delete claude/loose-ends-tasks-5uzjlk`.

## Notes

- The Claude Code chat where this project started remains listed under
  PRIMES-Conjectures; sessions can't be rehomed to another repo. Start future
  sessions from this repo.

## Possible next steps

- [ ] Seed the corpus with real sightings.
      *Tooling now exists (2026-09-10): tap Save on posts as you browse,
      request Meta's "Download your information" export (Instagram → Saved,
      JSON), then `./memepoetry.py ingest-export <export.zip>` — oEmbed
      fills in text/account; leftovers land in `pending.jsonl` for hand
      entry. What remains is actually doing the browsing + export.*
- [x] Instagram oEmbed-based URL canonicalizer, so pasted share links get
      normalized to the stable `/p/<shortcode>/` or `/reel/<shortcode>/` form.
      *Done 2026-09-08: `canonicalize_url()` runs on `add`/`import`/`lookup`,
      plus a `canon` subcommand and `--resolve` for opaque `/share/` links
      (oEmbed via `INSTAGRAM_OEMBED_TOKEN`, redirect fallback).*
