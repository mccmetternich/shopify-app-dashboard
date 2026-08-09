# Markdown twins and the JSON export

Two ways to get the dashboard out of the browser, built for different readers.

## The `.md` twin of every page

Every page has a twin at the same path (`/index.md`, `/customers.md`, `/reports/churn.md`), behind
the same auth. YAML frontmatter, prose explaining what each number means, then the data as fenced
JSON.

The Copy MD button puts the current page's twin on the clipboard, so a page can be pasted into an
agent as one prompt. Query params carry through: `/customers.md?install_state=uninstalled` exports
that filter.

Two rules hold in `markdown_export.py`, both tested:

- **No merchant contact details, ever.** There is one list of forbidden fields and it is asserted
  against, rather than each exporter being trusted to remember.
- **Every footnote caveat from the page is repeated in the prose.** A model that does not know
  deactivations are folded into uninstalls will confidently report the wrong churn number, and it
  will sound certain doing it. The caveat has to travel with the data or it does not exist.

## `GET /export.json`

The whole dashboard as one file, and deliberately *not* a twin of anything:

- **Widest window, not the reader's window.** A twin honours `?days=`; this takes the lot.
- **No silent truncation.** Display defaults in `stats.py` are overridden by `export.LIMITS`, which
  is written into `meta.windows` so a reader can tell a real end from a ceiling.
- **Unknown is `null` with a `note`, never `0`.** An empty activation list would read as "nobody
  activated", which is a much better story than the truth and a completely false one.
