# Archived: the classic pipeline's dashboard

Archived on 2026-11-14 when Roeland switched the dashboard to the transcript-first Shorts maker
(choice "everything to transcript-first"). Nothing here is imported or run any more.

- `web/`: the Flask blueprints as they were (home, make/review, schedule, library, sources, music,
  voices, channels, stats, bench, footage review, the page cache), plus copies of `__init__.py` and
  `common.py` at that moment.
- `templates/`: their templates, with the old `base.html` (old menu).
- `tests/`: the test files as they were before their dashboard tests were taken out of `tests/`.
- `meta/pagetime.py`: timed the old pages.

The classic pipeline itself (`shorts/` core modules, `python -m shorts make` and friends) is still
in place, because `meta/tclips.py` uses its database, upload, render and config code.

To bring the old dashboard back: move `web/*.py` back into `shorts/web/`, the templates into
`shorts/templates/` (old `base.html` too), restore the blueprint list and start-up steps from
`web/__init__.py`, and resume the channels (remove `paused = true` from their files, or the
Resume button once the pages are back). Git history has every file as well (commit f202c9f).
