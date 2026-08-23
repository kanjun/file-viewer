"""Browse and view files from the repo root with markdown rendering and pretty JSON.

The browse tree is rooted at the repo root. Anything outside that path is denied
(path-traversal guard). Markdown renders to HTML with code highlighting,
JSON is pretty-printed, and other text/code files render with Pygments
syntax highlighting. Binary or oversized files surface a download link
instead.

Services run from the repo root. The ``ROOT_PATH`` env var is read
so FastAPI emits prefix-aware absolute URLs when this app is reached
through the workspace_server proxy at ``/service/file-viewer/``.
"""

import datetime
import json
import os
import time
from pathlib import Path

import markdown as md
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer_for_filename
from pygments.util import ClassNotFound

ROOT_PATH = os.environ.get("ROOT_PATH", "")
# Browse tree is rooted at the repo root, resolved from this file's location
# (.../system/apps/file_viewer/src/file_viewer/runner.py -> parents[5]) so it
# works regardless of where the repo is checked out. ROOT_LABEL is the display
# label shown in breadcrumbs and path headers.
BROWSE_ROOT = Path(__file__).resolve().parents[5]
ROOT_LABEL = str(BROWSE_ROOT)
DEFAULT_ENTRY = "data"
MAX_RENDER_BYTES = 5 * 1024 * 1024  # 5 MB

_ASSETS = Path(__file__).parent / "assets"
_TEMPLATES_DIR = _ASSETS / "templates"
_STATIC_DIR = _ASSETS / "static"

app = FastAPI(title="file-viewer", root_path=ROOT_PATH)
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@app.get("/static/{path:path}", name="static", include_in_schema=False)
def serve_static(path: str) -> FileResponse:
    # Plain route instead of app.mount("/static", StaticFiles(...)) because
    # mounts get the root_path prefix baked into their effective URL, so
    # they only respond at /service/file-viewer/static/* on the backend;
    # the system_interface reverse proxy strips that prefix and forwards
    # /static/*, which the mount no longer matches. Regular routes match
    # at both the bare and prefixed paths.
    static_root = _STATIC_DIR.resolve()
    resolved = (static_root / path).resolve()
    if not resolved.is_relative_to(static_root) or not resolved.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(resolved)

STATIC_VERSION = str(int(time.time()))

_MARKDOWN_EXT = {".md", ".markdown", ".mdown", ".mkdn"}
_JSON_EXT = {".json", ".jsonl"}
_TEXT_EXT = {
    ".txt", ".log", ".rst", ".cfg", ".conf", ".ini", ".env",
    ".gitignore", ".gitattributes", ".editorconfig",
}
# Code files render via Pygments lexer-by-filename detection. Anything
# not in this allow-list is treated as binary and only offered as a raw
# download.
_CODE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".xml",
    ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    ".toml", ".yaml", ".yml",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".sql", ".graphql", ".proto",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".go", ".rb", ".java",
    ".kt", ".swift", ".php", ".pl", ".lua", ".r", ".jl", ".scala",
    ".dockerfile", ".makefile",
}


def _resolve_safe(path: str) -> Path:
    """Resolve a path relative to the browse root, rejecting traversal."""
    candidate = (BROWSE_ROOT / path.lstrip("/")).resolve() if path else BROWSE_ROOT
    try:
        candidate.relative_to(BROWSE_ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Path {path!r} is outside the browse root.")
    return candidate


def _breadcrumbs(path: Path) -> list[tuple[str, str]]:
    """List of (label, browse-path) pairs from root to this path."""
    rel = path.relative_to(BROWSE_ROOT)
    crumbs: list[tuple[str, str]] = [(ROOT_LABEL, "")]
    accumulated: list[str] = []
    for part in rel.parts:
        accumulated.append(part)
        crumbs.append((part, "/".join(accumulated)))
    return crumbs


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{n} B"
            return f"{n / 1024**('B KB MB GB'.split().index(unit)):.1f} {unit}"
        n //= 1
    return f"{n} B"


def _format_size_simple(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _format_mtime(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _render_directory(request: Request, target: Path) -> HTMLResponse:
    rel = target.relative_to(BROWSE_ROOT)
    entries = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            stat = entry.stat()
        except OSError:
            continue
        entries.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size": _format_size_simple(stat.st_size) if entry.is_file() else "",
            "mtime": _format_mtime(stat.st_mtime),
            "browse_path": str(rel / entry.name) if str(rel) != "." else entry.name,
        })
    parent_path = None
    if str(rel) != ".":
        parent = rel.parent
        parent_path = "" if str(parent) == "." else str(parent)
    return _templates.TemplateResponse(
        request=request,
        name="directory.html",
        context={
            "rel_path": ROOT_LABEL if str(rel) == "." else f"{ROOT_LABEL}/{rel}",
            "entries": entries,
            "breadcrumbs": _breadcrumbs(target),
            "parent_path": parent_path,
            "static_version": STATIC_VERSION,
        },
    )


def _render_markdown(text: str) -> str:
    return md.markdown(
        text,
        extensions=["extra", "sane_lists", "fenced_code", "codehilite", "toc", "tables"],
        extension_configs={"codehilite": {"css_class": "highlight", "guess_lang": False}},
    )


def _render_code(text: str, filename: str, hint_lang: str | None = None) -> str:
    if hint_lang:
        try:
            lexer = get_lexer_by_name(hint_lang)
        except ClassNotFound:
            lexer = TextLexer()
    else:
        try:
            lexer = guess_lexer_for_filename(filename, text)
        except ClassNotFound:
            lexer = TextLexer()
    formatter = HtmlFormatter(cssclass="highlight", linenos="table")
    return highlight(text, lexer, formatter)


def _render_file(request: Request, target: Path) -> HTMLResponse:
    rel = target.relative_to(BROWSE_ROOT)
    rel_str = str(rel)
    stat = target.stat()
    ext = target.suffix.lower()
    name_lower = target.name.lower()

    is_binary = False
    body_html = ""
    view_kind = "code"

    if stat.st_size > MAX_RENDER_BYTES:
        body_html = (
            f"<p class='notice'>File is {_format_size_simple(stat.st_size)} — "
            f"too large to render. Use the raw link to download.</p>"
        )
        view_kind = "binary"
    else:
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            is_binary = True

        if is_binary:
            body_html = (
                "<p class='notice'>Binary file — preview not available. "
                "Use the raw link to download.</p>"
            )
            view_kind = "binary"
        elif ext in _MARKDOWN_EXT:
            body_html = _render_markdown(text)
            view_kind = "markdown"
        elif ext in _JSON_EXT:
            try:
                parsed = json.loads(text)
                pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
                body_html = _render_code(pretty, target.name, hint_lang="json")
            except json.JSONDecodeError:
                body_html = _render_code(text, target.name, hint_lang="json")
            view_kind = "json"
        elif ext in _CODE_EXT or ext in _TEXT_EXT or name_lower in {"dockerfile", "makefile"}:
            body_html = _render_code(text, target.name)
            view_kind = "code"
        else:
            body_html = _render_code(text, target.name)
            view_kind = "code"

    return _templates.TemplateResponse(
        request=request,
        name="file.html",
        context={
            "rel_path": f"{ROOT_LABEL}/{rel_str}",
            "breadcrumbs": _breadcrumbs(target),
            "body_html": body_html,
            "view_kind": view_kind,
            "size": _format_size_simple(stat.st_size),
            "mtime": _format_mtime(stat.st_mtime),
            "raw_url": request.url_for("raw", path=rel_str).path,
            "pygments_css": HtmlFormatter(cssclass="highlight").get_style_defs(".highlight"),
            "static_version": STATIC_VERSION,
        },
    )


@app.get("/", include_in_schema=False)
def index(request: Request) -> RedirectResponse:
    # Path-only Location (with root_path prefix) so the browser resolves
    # against its own host, not the backend's localhost:8084 which would be
    # unreachable from a remote client behind the system_interface proxy.
    target = (BROWSE_ROOT / DEFAULT_ENTRY).resolve()
    if target.exists():
        return RedirectResponse(url=request.url_for("browse", path=DEFAULT_ENTRY).path)
    return RedirectResponse(url=request.url_for("browse_root").path)


@app.get("/browse", name="browse_root", response_class=HTMLResponse)
def browse_root(request: Request) -> HTMLResponse:
    return _render_directory(request, BROWSE_ROOT)


@app.get("/browse/{path:path}", name="browse", response_class=HTMLResponse)
def browse(request: Request, path: str) -> HTMLResponse:
    target = _resolve_safe(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"{path!r} not found.")
    if target.is_dir():
        return _render_directory(request, target)
    return _render_file(request, target)


@app.get("/raw/{path:path}", name="raw")
def raw(path: str) -> FileResponse:
    target = _resolve_safe(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"{path!r} is not a file.")
    return FileResponse(str(target), filename=target.name)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8084)


if __name__ == "__main__":
    main()
