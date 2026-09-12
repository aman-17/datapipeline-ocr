"""A local HTTP server over the index, thumbnail cache and selection.

Standard-library only: the viewer is a single-user tool on the machine that
holds the corpus, and adding a web framework to the collection package for
eight routes is not worth the dependency. Threaded so thumbnail cache hits and
the UI stay responsive while one render holds the pdfium lock.
"""
from __future__ import annotations

import gzip
import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .index import PAGE_FILTERS, DocIndex
from .selection import Selection
from .thumbs import THUMB_WIDTH, ThumbCache

UI_PATH = Path(__file__).with_name("ui.html")
PAGE_LIMIT = 200


class App:
    def __init__(self, index: DocIndex, thumbs: ThumbCache, selection: Selection):
        self.index = index
        self.thumbs = thumbs
        self.selection = selection

    def doc(self, doc_id: int) -> dict[str, Any] | None:
        return self.index.docs.get(doc_id)

    def selected_map(self) -> dict[str, list[int]]:
        return {sha: sorted(d["pages"]) for sha, d in self.selection.docs.items()}

    def api_docs(self) -> dict[str, Any]:
        # 8k rows go to the browser in one shot; the stored path is the one
        # field it never needs and the longest, so it stays server-side.
        docs = [{k: v for k, v in d.items() if k != "path"} for d in self.index.docs.values()]
        return {"summary": self.index.summary(), "docs": docs,
                "selected": self.selected_map(), "out_dir": str(self.selection.out_dir),
                "page_filters": list(PAGE_FILTERS)}

    def api_doc(self, doc_id: int) -> dict[str, Any] | None:
        doc = self.doc(doc_id)
        if doc is None:
            return None
        return {"doc": doc, "n_pages": self.index.page_count(doc),
                "pages": self.index.page_signals(doc_id),
                "selected": self.selection.pages_of(doc["sha"])}

    def api_pages(self, q: dict[str, list[str]]) -> dict[str, Any]:
        source = (q.get("source") or [""])[0] or None
        filters = [f for f in (q.get("f") or [""])[0].split(",") if f]
        limit = min(int((q.get("limit") or [PAGE_LIMIT])[0]), PAGE_LIMIT)
        offset = int((q.get("offset") or ["0"])[0])
        total, rows = self.index.find_pages(source=source, filters=filters,
                                            limit=limit, offset=offset)
        items = []
        for r in rows:
            d = self.index.docs[r["doc_id"]]
            items.append({"doc_id": d["id"], "sha": d["sha"], "page_no": r["page_no"],
                          "source": d["source"], "title": d["title"]})
        return {"total": total, "offset": offset, "limit": limit, "items": items,
                "selected": self.selected_map()}

    def api_selection(self) -> dict[str, Any]:
        docs = []
        for sha, d in sorted(self.selection.docs.items(), key=lambda kv: (kv[1]["source"], kv[0])):
            docs.append({"sha": sha, "id": self.index.by_sha.get(sha), "source": d["source"],
                         "title": d["title"], "pages": sorted(d["pages"])})
        return {"count": self.selection.count(), "docs": docs,
                "out_dir": str(self.selection.out_dir)}

    def api_select(self, body: dict[str, Any]) -> dict[str, Any] | None:
        doc = self.doc(int(body["doc_id"]))
        if doc is None:
            return None
        pages = sorted({int(p) for p in body["pages"]})
        n = self.index.page_count(doc) or 0
        pages = [p for p in pages if 1 <= p <= n]
        have = self.selection.set(doc, pages, bool(body["selected"]))
        return {"sha": doc["sha"], "pages": have, "count": self.selection.count()}


class Handler(BaseHTTPRequestHandler):
    app: App
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 — quiet on success
        if len(args) > 1 and str(args[1])[:1] in "45":
            super().log_message(fmt, *args)

    # ---- helpers ----------------------------------------------------------------
    def _send(self, status: HTTPStatus, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        if len(body) > 8192 and "gzip" in self.headers.get("Accept-Encoding", "") \
                and not ctype.startswith("image/"):
            body = gzip.compress(body, compresslevel=5)
            extra = {**(extra or {}), "Content-Encoding": "gzip"}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json",
                   {"Cache-Control": "no-store"})

    def _error(self, status: HTTPStatus, msg: str) -> None:
        self._json({"error": msg}, status)

    # ---- routing ----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        app = self.app
        try:
            if not parts:
                self._send(HTTPStatus.OK, UI_PATH.read_bytes(), "text/html; charset=utf-8",
                           {"Cache-Control": "no-store"})
            elif parts[:2] == ["api", "docs"]:
                self._json(app.api_docs())
            elif parts[:2] == ["api", "doc"] and len(parts) == 3:
                out = app.api_doc(int(parts[2]))
                self._json(out) if out else self._error(HTTPStatus.NOT_FOUND, "no such document")
            elif parts[:2] == ["api", "pages"]:
                self._json(app.api_pages(q))
            elif parts[:2] == ["api", "selection"]:
                self._json(app.api_selection())
            elif parts[0] == "thumb" and len(parts) == 3:
                doc_id = app.index.by_sha.get(parts[1])
                doc = app.doc(doc_id) if doc_id is not None else None
                if doc is None:
                    return self._error(HTTPStatus.NOT_FOUND, "no such document")
                width = int((q.get("w") or [THUMB_WIDTH])[0])
                data = app.thumbs.get(doc["path"], doc["sha"], int(parts[2]), width)
                self._send(HTTPStatus.OK, data, "image/jpeg",
                           {"Cache-Control": "public, max-age=31536000, immutable"})
            elif parts[0] == "pdf" and len(parts) == 2:
                doc_id = app.index.by_sha.get(parts[1])
                doc = app.doc(doc_id) if doc_id is not None else None
                if doc is None:
                    return self._error(HTTPStatus.NOT_FOUND, "no such document")
                self._send(HTTPStatus.OK, Path(doc["path"]).read_bytes(), "application/pdf",
                           {"Content-Disposition": f'inline; filename="{doc["sha"][:16]}.pdf"'})
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except (ValueError, KeyError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if url.path == "/api/select":
                out = self.app.api_select(body)
                self._json(out) if out else self._error(HTTPStatus.NOT_FOUND, "no such document")
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except (ValueError, KeyError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface extraction failures to the UI
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")


def serve(app: App, *, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> None:
    mimetypes.init()
    Handler.app = app
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise SystemExit(f"cannot listen on {host}:{port} ({exc.strerror}); "
                         f"is a viewer already running? pass --port to use another") from exc
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"  viewer at {url}   (Ctrl-C to stop)")
    print(f"  selected pages land in {app.selection.out_dir}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
