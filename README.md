# file-viewer

A small FastAPI file browser: browse a directory tree, render markdown to HTML,
pretty-print JSON, and syntax-highlight code (via Pygments). Binary or oversized
files surface a download link instead.

## What's here

- `src/file_viewer/assets/static/style.css` — the stylesheet (the styling you're
  probably here for).
- `src/file_viewer/assets/templates/` — the Jinja2 templates the CSS styles
  (`base.html`, `directory.html`, `file.html`).
- `src/file_viewer/runner.py` — the FastAPI server.

## Running it

```bash
uv run --with fastapi --with uvicorn --with jinja2 --with markdown --with pygments \
  python -m file_viewer.runner
```

Then open http://127.0.0.1:8084.

Note: `runner.py` roots the browse tree at a fixed relative parent of its own
location (it was extracted from a larger monorepo). If you run it standalone,
adjust `BROWSE_ROOT` in `runner.py` to the directory you want to browse.
