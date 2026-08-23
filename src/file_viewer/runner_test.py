from fastapi.testclient import TestClient

from file_viewer.runner import app


def test_index_redirect_is_host_relative() -> None:
    # The "/" redirect must be a path, not an absolute URL pointing at the
    # backend's own host. A remote browser reaches this app through the
    # system_interface proxy, so an absolute http://127.0.0.1:8084/... Location
    # would be unreachable.
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("/")
    assert "://" not in location


def test_directory_links_are_host_relative() -> None:
    # Regression guard: templates must use url_for(...).path, not a bare
    # url_for(...), or Starlette renders absolute URLs (http://testserver/...
    # under test, http://127.0.0.1:8084/... in prod) that break navigation and
    # CSS when the page is served through the reverse proxy.
    client = TestClient(app)
    resp = client.get("/browse", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "http://testserver" not in body
    # Stylesheet and breadcrumb/listing links should be present and root-relative.
    assert "/static/style.css" in body
    assert 'href="/' in body
