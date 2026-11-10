"""Administrator audit history, bounded continuation and dashboard separation."""
from html.parser import HTMLParser

import pytest

from election_system import db as database
from election_system.db import get_db

from .helpers import ADMIN_PASSWORD, VOTER_PASSWORD, login


AUDIT_URL = "/admin/audit-logs"
ENTRIES_URL = AUDIT_URL + "/entries"


class AuditPage(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.entries = []
        self.current_entry = None
        self.more = None
        self.feed(page.decode())

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "li" and "data-audit-entry" in attrs:
            self.current_entry = []
        if attrs.get("id") == "load-more-audit-logs":
            self.more = attrs

    def handle_data(self, data):
        if self.current_entry is not None:
            self.current_entry.append(data)

    def handle_endtag(self, tag):
        if tag == "li" and self.current_entry is not None:
            self.entries.append("".join(self.current_entry))
            self.current_entry = None


def seed_logs(client, count):
    with client.application.app_context():
        with get_db() as db:
            db.execute("DELETE FROM audit_logs")
            db.executemany(
                "INSERT INTO audit_logs (id, admin_id, action, timestamp) VALUES (?, 1, ?, ?)",
                [(index, f"Audit action {index:04d}", "2026-11-04 12:00:00")
                 for index in range(1, count + 1)],
            )


def has_more(page):
    return page.more is not None and "hidden" not in page.more


@pytest.mark.parametrize("count", [0, 20, 21, 45])
def test_initial_history_and_continuation_reach_every_entry_once(client, count):
    seed_logs(client, count)
    login(client, "admin", ADMIN_PASSWORD)
    response = client.get(AUDIT_URL)
    assert response.status_code == 200
    page = AuditPage(response.data)
    assert len(page.entries) == min(count, 20)
    for entry, index in zip(page.entries, range(count, max(0, count - 20), -1)):
        assert f"Audit action {index:04d}" in entry
        assert "admin" in entry
        assert "2026-11-04 12:00:00" in entry
    assert has_more(page) == (count > 20)
    if count == 0:
        assert b"No admin actions recorded yet." in response.data

    expected = [f"Audit action {index:04d}" for index in range(count, 0, -1)]
    remaining = expected[20:]
    next_url = page.more.get("data-next-url") if has_more(page) else None
    batch_sizes = []
    while next_url:
        response = client.get(next_url)
        assert response.status_code == 200
        assert response.is_json
        batch = response.get_json()
        actions = [entry["action"] for entry in batch["entries"]]
        assert actions == remaining[:20]
        assert all(entry["username"] == "admin" for entry in batch["entries"])
        assert all(entry["timestamp"] == "2026-11-04 12:00:00" for entry in batch["entries"])
        batch_sizes.append(len(actions))
        remaining = remaining[20:]
        next_url = batch["next_url"]
        assert bool(next_url) == bool(remaining)
    assert not remaining
    assert batch_sizes == ({21: [1], 45: [20, 5]}.get(count, []))


def test_continuation_is_stable_when_newer_actions_are_added(client):
    seed_logs(client, 45)
    login(client, "admin", ADMIN_PASSWORD)
    page = AuditPage(client.get(AUDIT_URL).data)
    with client.application.app_context():
        with get_db():
            database.log_audit(1, "Added after history was opened")

    response = client.get(page.more["data-next-url"])
    batch = response.get_json()
    assert [entry["action"] for entry in batch["entries"]] == [
        f"Audit action {index:04d}" for index in range(25, 5, -1)
    ]
    last_batch = client.get(batch["next_url"]).get_json()
    assert [entry["action"] for entry in last_batch["entries"]] == [
        f"Audit action {index:04d}" for index in range(5, 0, -1)
    ]
    assert last_batch["next_url"] is None
    refreshed = AuditPage(client.get(AUDIT_URL).data)
    assert "Added after history was opened" in refreshed.entries[0]


@pytest.mark.parametrize("before", [
    None, "", "0", "-1", "+1", "1.5", "1e2", "abc", "1 OR 1=1", " 1 ",
    "１", "١", "9223372036854775808", "9" * 5000,
])
def test_invalid_cursor_returns_json_bad_request(client, before):
    login(client, "admin", ADMIN_PASSWORD)
    response = client.get(ENTRIES_URL, query_string={} if before is None else {"before": before})
    assert response.status_code == 400
    assert response.is_json
    assert "error" in response.get_json()


def test_exhausted_and_maximum_valid_cursors(client):
    seed_logs(client, 3)
    login(client, "admin", ADMIN_PASSWORD)
    assert client.get(ENTRIES_URL, query_string={"before": "1"}).get_json() == {
        "entries": [], "next_url": None,
    }
    response = client.get(ENTRIES_URL, query_string={"before": "9223372036854775807"})
    assert response.status_code == 200
    assert [entry["action"] for entry in response.get_json()["entries"]] == [
        "Audit action 0003", "Audit action 0002", "Audit action 0001",
    ]
    assert response.get_json()["next_url"] is None


@pytest.mark.parametrize("username", [None, "voter1"])
@pytest.mark.parametrize("url", [AUDIT_URL, ENTRIES_URL + "?before=2"])
def test_history_routes_require_an_administrator(client, username, url):
    seed_logs(client, 1)
    if username:
        login(client, username, VOTER_PASSWORD)
    response = client.get(url)
    assert response.status_code == 302
    assert response.headers["Location"] == ("/" if username else "/login")
    assert b"Audit action" not in response.data


@pytest.mark.parametrize("username", [None, "voter1", "admin"])
def test_audit_navigation_is_visible_only_to_administrators(client, username):
    if username:
        login(client, username, ADMIN_PASSWORD if username == "admin" else VOTER_PASSWORD)
    urls = ["/", "/election/1"]
    if username == "admin":
        urls.extend(["/admin/election/create", AUDIT_URL])
    for url in urls:
        response = client.get(url, follow_redirects=True)
        assert (b'href="/admin/audit-logs"' in response.data) == (username == "admin")


def test_initial_history_escapes_fields_and_batch_returns_literal_text(client):
    seed_logs(client, 21)
    login(client, "admin", ADMIN_PASSWORD)
    unsafe = '<img src=x onerror="alert(1)"> & <script>alert(2)</script>'
    with client.application.app_context():
        with get_db() as db:
            db.execute("UPDATE users SET username = ? WHERE id = 1", (unsafe,))
            db.execute("UPDATE audit_logs SET action = ?, timestamp = ? WHERE id IN (1, 21)",
                       (unsafe, unsafe))
    response = client.get(AUDIT_URL)
    assert unsafe.encode() not in response.data
    assert b"&lt;img" in response.data and b"&lt;script&gt;" in response.data
    page = AuditPage(response.data)
    batch = client.get(page.more["data-next-url"])
    assert batch.is_json
    assert batch.get_json() == {
        "entries": [{"username": unsafe, "action": unsafe, "timestamp": unsafe}],
        "next_url": None,
    }


def test_dashboard_has_full_width_elections_without_loading_audit_history(client, monkeypatch):
    seed_logs(client, 1)
    login(client, "admin", ADMIN_PASSWORD)

    def unexpected_audit_query(*args, **kwargs):
        raise AssertionError("Dashboard must not query audit history")

    monkeypatch.setattr(database, "list_recent_audit_logs", unexpected_audit_query)
    monkeypatch.setattr(database, "list_audit_logs", unexpected_audit_query)
    response = client.get("/")
    assert response.status_code == 200
    assert b'class="col-12"' in response.data
    assert b'href="/election/1"' in response.data
    assert b"Administrative Audit Log" not in response.data
    assert b"Audit action 0001" not in response.data


def test_existing_recent_history_query_keeps_latest_fifteen_contract(client):
    seed_logs(client, 21)
    with client.application.app_context():
        logs = database.list_recent_audit_logs()
        assert [entry["id"] for entry in logs] == list(range(21, 6, -1))
