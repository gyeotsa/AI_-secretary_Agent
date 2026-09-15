"""Offline pagination oracles: never construct a user vault or use a live account."""
from __future__ import annotations

import base64
import json

import pytest
import requests

from core.plugin import BasePlugin, ToolCancelledError
from core.remote_runtime import CatalogSyncResult, ProviderApi, RemoteCatalogStore, RemoteRuntimeError
from core.tool_result import ToolRunStatus
from plugins.cloud_communication import CloudCommunicationPlugin


GRAPH = "https://graph.microsoft.com/v1.0/me/drive/root/delta"


def test_cloud_diagnostics_do_not_claim_authentication_is_unnecessary():
    plugin = object.__new__(CloudCommunicationPlugin)
    BasePlugin.__init__(plugin)
    assert plugin.probe_connection().state == "unchecked"
    assert plugin.probe_authentication().state == "unchecked"


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.text = "secret-response-body-must-not-be-reported"
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False
        assert 0 < kwargs["timeout"] <= 15
        assert self.replies, "Unexpected extra remote request"
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class OAuth:
    def __init__(self):
        self.calls = []

    def access_token(self, provider, account):
        self.calls.append((provider, account))
        return "fixture-bearer-only"


def page(provider, items, token=None, **extra):
    """Fixture protocol is independent of production cursor parsing."""
    if provider == "google_drive":
        payload = {"files": items, "incompleteSearch": False}
        if token is not None:
            payload["nextPageToken"] = token
    elif provider == "notion":
        payload = {"results": items, "has_more": token is not None, "next_cursor": token}
    else:
        payload = {"value": items,
                   "@odata.nextLink" if token is not None else "@odata.deltaLink":
                   GRAPH + ("?$skiptoken=" + token if token is not None else "?token=final-state")}
    return Response({**payload, **extra})


def plugin_for(tmp_path, session, *, provider="google_drive"):
    # Avoid CloudCommunicationPlugin.__init__: it intentionally creates the
    # user's token/action stores. These tests need only an isolated catalog.
    plugin = object.__new__(CloudCommunicationPlugin)
    BasePlugin.__init__(plugin)
    plugin.catalog = RemoteCatalogStore(str(tmp_path / "catalog.db"))
    plugin.api = ProviderApi(OAuth(), session)
    plugin.catalog.replace(provider, "account-a", [{"id": "old", "name": "keep-until-full-scan"}])
    plugin.catalog.replace(provider, "account-b", [{"id": "foreign", "name": "other-account"}])
    return plugin


def ids(catalog, provider="google_drive", account="account-a"):
    return {item["id"] for item in catalog.list(provider, account)}


@pytest.mark.parametrize("provider", ["google_drive", "onedrive", "notion"])
def test_complete_fresh_scan_reads_all_pages_and_replaces_only_its_account(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("NOTION_API_TOKEN", "fixture-notion-only")
    first = [{"id": f"item-{number}"} for number in range(100)]
    second = [{"id": f"item-{number}"} for number in range(100, 200)]
    third = [{"id": "item-200", "name": "last"}]
    replies = [page(provider, first, "page-two"), page(provider, second, "page-three"), page(provider, third)]
    session = Session(*replies)
    plugin = plugin_for(tmp_path, session, provider=provider)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": provider, "account": "account-a", "limit": 100})
    assert result.succeeded
    assert ids(plugin.catalog, provider) == {f"item-{number}" for number in range(201)}
    assert ids(plugin.catalog, provider, "account-b") == {"foreign"}
    detail = result.evidence[0].data
    assert detail["snapshot_complete"] and detail["pagination_complete"]
    assert detail["pages_fetched"] == 3 and detail["catalog_mode"] == "replaced"
    assert not detail["content_synced"] and detail["next_cursor"] == ""
    assert all(reply.closed for reply in replies)
    if provider == "google_drive":
        assert "nextPageToken" in session.calls[0][2]["params"]["fields"]
        assert "incompleteSearch" in session.calls[0][2]["params"]["fields"]
        assert session.calls[1][2]["params"]["pageToken"] == "page-two"
    elif provider == "notion":
        assert session.calls[1][2]["json"]["start_cursor"] == "page-two"
    else:
        assert session.calls[1][1] == GRAPH + "?$skiptoken=page-two"
        assert "params" not in session.calls[1][2]


@pytest.mark.parametrize("provider", ["google_drive", "onedrive", "notion"])
def test_bounded_scan_and_terminal_resume_remain_non_destructive(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("NOTION_API_TOKEN", "fixture-notion-only")
    session = Session(page(provider, [{"id": "first"}], "next-page"), page(provider, [{"id": "last"}]))
    plugin = plugin_for(tmp_path, session, provider=provider)
    request = {"provider": provider, "account": "account-a", "max_pages": 1}
    first = plugin.execute_tool("cloud_sync_catalog", request)
    assert first.status is ToolRunStatus.PARTIAL and not first.succeeded
    assert ids(plugin.catalog, provider) == {"old", "first"}
    cursor = first.evidence[0].data["next_cursor"]
    assert cursor and cursor != "next-page" and not cursor.startswith("https:")
    second = plugin.execute_tool("cloud_sync_catalog", {**request, "cursor": cursor})
    assert second.status is ToolRunStatus.PARTIAL
    assert ids(plugin.catalog, provider) == {"old", "first", "last"}
    assert second.evidence[0].data["pagination_complete"]
    assert not second.evidence[0].data["snapshot_complete"]
    assert second.evidence[0].data["resumed"]


@pytest.mark.parametrize("failure", [Response({}, 503), requests.Timeout("secret-url"), ValueError("secret-body")])
def test_second_page_failure_upserts_observed_items_and_retains_retry_cursor(tmp_path, failure):
    session = Session(page("google_drive", [{"id": "new"}], "retry-this-page"), failure)
    plugin = plugin_for(tmp_path, session)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.status is ToolRunStatus.PARTIAL
    assert ids(plugin.catalog) == {"old", "new"}
    detail = result.evidence[0].data
    assert detail["pages_fetched"] == 1 and not detail["pagination_complete"]
    assert detail["next_cursor"] and detail["error"]
    assert "secret" not in json.dumps(result.to_dict())
    resumed = Session(page("google_drive", [{"id": "tail"}]))
    again = ProviderApi(OAuth(), resumed).sync("google_drive", "account-a", {"cursor": detail["next_cursor"]})
    assert resumed.calls[0][2]["params"]["pageToken"] == "retry-this-page"
    assert again.complete and not again.full_snapshot


@pytest.mark.parametrize("provider,payload", [
    ("google_drive", {}), ("google_drive", {"files": None}),
    ("google_drive", {"files": [{"id": "valid"}, {"name": "missing-id"}]}),
    ("google_drive", {"files": [{"id": 123}]}),
    ("google_drive", {"files": [], "nextPageToken": ""}),
    ("google_drive", {"files": [], "incompleteSearch": "false"}),
    ("notion", {"results": []}),
    ("notion", {"results": [], "has_more": True, "next_cursor": None}),
    ("notion", {"results": [], "has_more": False, "next_cursor": "unexpected"}),
    ("onedrive", {"value": []}),
    ("onedrive", {"value": [], "@odata.nextLink": GRAPH + "?next=x", "@odata.deltaLink": GRAPH + "?end=y"}),
])
def test_malformed_pages_never_clear_previous_catalog(tmp_path, monkeypatch, provider, payload):
    monkeypatch.setenv("NOTION_API_TOKEN", "fixture-notion-only")
    plugin = plugin_for(tmp_path, Session(Response(payload)), provider=provider)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": provider, "account": "account-a"})
    assert not result.succeeded
    assert ids(plugin.catalog, provider) == {"old"}
    assert result.evidence[0].data["catalog_mode"] != "replaced"


@pytest.mark.parametrize("url", [
    "http://graph.microsoft.com/v1.0/me/drive/root/delta?x=1",
    "https://graph.microsoft.com.evil.test/v1.0/me/drive/root/delta?x=1",
    "https://graph.microsoft.com@evil.test/v1.0/me/drive/root/delta?x=1",
    "https://user:pass@graph.microsoft.com/v1.0/me/drive/root/delta?x=1",
    "https://graph.microsoft.com:444/v1.0/me/drive/root/delta?x=1",
    "https://graph.microsoft.com/v1.0/users/other/drive/root/delta?x=1",
    "https://graph.microsoft.com/v1.0/me/drive/root/delta/../content?x=1",
    "https://graph.microsoft.com/v1.0/me/drive/root/delta?x=1#fragment",
    "https://graph.microsoft.com\\@evil.test/v1.0/me/drive/root/delta?x=1",
    "https://graph.microsoft.com/v1.0/me/drive/root/delta?x=1\n",
    "/v1.0/me/drive/root/delta?x=1",
    "https://127.0.0.1/v1.0/me/drive/root/delta?x=1",
])
def test_untrusted_graph_next_link_never_receives_bearer(tmp_path, url):
    session = Session(Response({"value": [{"id": "observed"}], "@odata.nextLink": url}))
    plugin = plugin_for(tmp_path, session, provider="onedrive")
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "onedrive", "account": "account-a"})
    assert result.status is ToolRunStatus.PARTIAL
    assert len(session.calls) == 1 and len(plugin.api.oauth.calls) == 1
    assert ids(plugin.catalog, "onedrive") == {"old", "observed"}
    assert url not in result.raw_output


@pytest.mark.parametrize("provider", ["google_drive", "onedrive", "notion"])
def test_redirect_is_not_followed_even_for_first_page(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("NOTION_API_TOKEN", "fixture-notion-only")
    response = Response({}, 302)
    response.headers = {"Location": "https://evil.test/collect-token"}
    session = Session(response)
    plugin = plugin_for(tmp_path, session, provider=provider)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": provider, "account": "account-a"})
    assert not result.succeeded and len(session.calls) == 1 and response.closed
    assert ids(plugin.catalog, provider) == {"old"}


@pytest.mark.parametrize("change", [{"provider": "notion"}, {"account": "account-b"}, {"query": "other"}])
def test_cursor_cannot_cross_provider_account_or_query(change):
    api = ProviderApi(OAuth(), Session(page("google_drive", [], "next")))
    result = api.sync("google_drive", "account-a", {"max_pages": 1})
    target = {"provider": "google_drive", "account": "account-a", "query": "", **change}
    session, oauth = Session(), OAuth()
    with pytest.raises(RemoteRuntimeError):
        ProviderApi(oauth, session).sync(target["provider"], target["account"], {
            "cursor": result.next_cursor, "query": target["query"],
        })
    assert not session.calls and not oauth.calls


@pytest.mark.parametrize("cursor", [None, 3, "not base64!", "e30", "A" * 65537],
                         ids=["null", "number", "invalid-base64", "empty-object", "too-long"])
def test_malformed_incoming_cursor_makes_no_request(cursor):
    session, oauth = Session(), OAuth()
    with pytest.raises(RemoteRuntimeError):
        ProviderApi(oauth, session).sync("google_drive", "account-a", {"cursor": cursor})
    assert not session.calls and not oauth.calls


@pytest.mark.parametrize("provider", ["google_drive", "notion"])
def test_completed_query_is_merge_only_and_scope_is_visible(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("NOTION_API_TOKEN", "fixture-notion-only")
    session = Session(page(provider, [{"id": "matched"}]))
    plugin = plugin_for(tmp_path, session, provider=provider)
    result = plugin.execute_tool("cloud_sync_catalog", {
        "provider": provider, "account": "account-a", "query": "report",
    })
    assert result.status is ToolRunStatus.PARTIAL
    assert ids(plugin.catalog, provider) == {"old", "matched"}
    assert result.evidence[0].data["scope"] == "filtered_metadata"
    assert result.evidence[0].data["pagination_complete"]
    kwargs = session.calls[0][2]
    assert kwargs["params"]["q"] == "report" if provider == "google_drive" else kwargs["json"]["query"] == "report"


@pytest.mark.parametrize("options", [{"q": "scoped"}, {"limit": True}, {"max_pages": 0}, {"max_pages": 11},
                                      {"limit": 101}, {"query": " "}, {"account": "another-account"}])
def test_unknown_or_invalid_scope_budget_is_not_silently_ignored(options):
    session = Session()
    with pytest.raises(RemoteRuntimeError):
        ProviderApi(OAuth(), session).sync("google_drive", "account-a", options)
    assert not session.calls


def test_onedrive_query_is_rejected_before_oauth():
    session, oauth = Session(), OAuth()
    with pytest.raises(RemoteRuntimeError):
        ProviderApi(oauth, session).sync("onedrive", "account-a", {"query": "filtered"})
    assert not session.calls and not oauth.calls


def test_provider_incomplete_search_never_becomes_full_snapshot(tmp_path):
    plugin = plugin_for(tmp_path, Session(page("google_drive", [{"id": "new"}], incompleteSearch=True)))
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.status is ToolRunStatus.PARTIAL and ids(plugin.catalog) == {"old", "new"}
    assert result.evidence[0].data["incomplete_search"]


def test_repeated_cursor_stops_with_partial_non_destructive_result(tmp_path):
    session = Session(page("google_drive", [{"id": "one"}], "same"),
                      page("google_drive", [{"id": "two"}], "same"))
    plugin = plugin_for(tmp_path, session)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.status is ToolRunStatus.PARTIAL and len(session.calls) == 2
    assert ids(plugin.catalog) == {"old", "one", "two"}


def test_empty_terminal_full_scan_can_clear_only_its_account(tmp_path):
    plugin = plugin_for(tmp_path, Session(page("google_drive", [])))
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.succeeded and ids(plugin.catalog) == set()
    assert ids(plugin.catalog, account="account-b") == {"foreign"}


def test_onedrive_latest_duplicate_and_tombstone_win_within_full_scan(tmp_path):
    session = Session(page("onedrive", [{"id": "gone", "name": "before"}, {"id": "live", "name": "old"}], "next"),
                      page("onedrive", [{"id": "gone", "deleted": {}}, {"id": "live", "name": "latest"}]))
    plugin = plugin_for(tmp_path, session, provider="onedrive")
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "onedrive", "account": "account-a"})
    assert result.succeeded
    assert plugin.catalog.list("onedrive", "account-a") == [{"id": "live", "name": "latest"}]


def test_incomplete_onedrive_tombstone_does_not_delete_existing_item(tmp_path):
    session = Session(page("onedrive", [{"id": "old", "deleted": {}}], "next"))
    plugin = plugin_for(tmp_path, session, provider="onedrive")
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "onedrive", "account": "account-a", "max_pages": 1})
    assert result.status is ToolRunStatus.PARTIAL and ids(plugin.catalog, "onedrive") == {"old"}
    assert result.evidence[0].data["deletions_deferred"]


def test_cancellation_between_pages_never_fetches_next_or_publishes(tmp_path):
    session = Session(page("google_drive", [{"id": "first"}], "next"))
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise ToolCancelledError("cancel")

    api = ProviderApi(OAuth(), session)
    result = api.sync("google_drive", "account-a", {}, check_cancelled=cancel)
    assert result.cancelled and not result.complete and not result.full_snapshot
    assert [item["id"] for item in result] == ["first"] and len(session.calls) == 1
    plugin = plugin_for(tmp_path, Session())
    plugin.api = type("CancelledApi", (), {"sync": lambda *_args, **_kwargs: result})()
    delivered = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert delivered.status is ToolRunStatus.CANCELLED and ids(plugin.catalog) == {"old"}


def test_cancel_after_sql_write_rolls_back_replace_and_revision(tmp_path):
    store = RemoteCatalogStore(str(tmp_path / "rollback.db"))
    store.replace("google_drive", "account-a", [{"id": "old"}])
    revision = store.revision("google_drive", "account-a")
    result = ProviderApi(OAuth(), Session(page("google_drive", [{"id": "new"}]))).sync("google_drive", "account-a", {})
    checks = 0

    def cancel_during_publication():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise ToolCancelledError("cancel before commit")

    with pytest.raises(ToolCancelledError):
        store.apply_sync("google_drive", "account-a", result, expected_revision=revision,
                         check_cancelled=cancel_during_publication)
    assert ids(store) == {"old"} and store.revision("google_drive", "account-a") == revision


def test_concurrent_new_catalog_prevents_old_scan_overwrite(tmp_path):
    plugin = plugin_for(tmp_path, Session())

    def later_snapshot():
        plugin.catalog.replace("google_drive", "account-a", [{"id": "newer-writer"}])
        return page("google_drive", [{"id": "stale-response"}])

    plugin.api.session = Session(later_snapshot)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.status is ToolRunStatus.FAILED
    assert ids(plugin.catalog) == {"newer-writer"}
    assert result.evidence[0].data["catalog_mode"] == "unchanged"


def test_legacy_plain_list_is_not_full_snapshot_evidence(tmp_path):
    plugin = plugin_for(tmp_path, Session())
    plugin.api = type("LegacyApi", (), {"sync": lambda *_args, **_kwargs: [{"id": "bare-list"}]})()
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert not result.succeeded and ids(plugin.catalog) == {"old"}


def test_cursor_with_forged_graph_url_cannot_fetch_or_replace():
    result = ProviderApi(OAuth(), Session(page("onedrive", [], "next"))).sync("onedrive", "account-a", {"max_pages": 1})
    payload = json.loads(base64.urlsafe_b64decode(result.next_cursor + "=" * (-len(result.next_cursor) % 4)))
    payload["page"] = "https://evil.test/collect"
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    session, oauth = Session(), OAuth()
    with pytest.raises(RemoteRuntimeError):
        ProviderApi(oauth, session).sync("onedrive", "account-a", {"cursor": cursor})
    assert not session.calls and not oauth.calls


def test_catalog_rejects_wrong_result_owner_and_invalid_item_before_delete(tmp_path):
    store = RemoteCatalogStore(str(tmp_path / "validation.db"))
    store.replace("google_drive", "account-a", [{"id": "old"}])
    revision = store.revision("google_drive", "account-a")
    wrong = CatalogSyncResult("google_drive", "different")
    wrong.complete = True
    with pytest.raises(RemoteRuntimeError):
        store.apply_sync("google_drive", "account-a", wrong, expected_revision=revision)
    malformed = CatalogSyncResult("google_drive", "account-a")
    malformed.complete = True
    malformed.extend([{"id": "valid"}, {"name": "invalid"}])
    with pytest.raises(RemoteRuntimeError):
        store.apply_sync("google_drive", "account-a", malformed, expected_revision=revision)
    assert ids(store) == {"old"}


def test_sync_schema_has_bounded_budget_and_opaque_cursor():
    plugin = object.__new__(CloudCommunicationPlugin)
    BasePlugin.__init__(plugin)
    schema = next(item for item in plugin.get_tools() if item.name == "cloud_sync_catalog")
    assert schema.timeout_seconds == 90 and schema.cancellable and schema.max_retries == 0
    assert schema.input_schema["properties"]["max_pages"]["maximum"] == 10
    assert schema.input_schema["properties"]["cursor"]["type"] == "string"


@pytest.mark.parametrize("next_url", [
    "https://graph.microsoft.com/v1.0/me/drive/delta(token=1230919asd190410jlka)",
    "https://graph.microsoft.com/v1.0/me/drive/delta(token='encoded%2Ftoken%3D')",
    "https://graph.microsoft.com/v1.0/me/drive/root/delta?$skiptoken=opaque",
])
def test_documented_graph_delta_navigation_forms_are_followed_exactly(next_url):
    session = Session(Response({"value": [{"id": "one"}], "@odata.nextLink": next_url}),
                      Response({"value": [{"id": "two"}],
                                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/drive/delta(token=latest-state)"}))
    result = ProviderApi(OAuth(), session).sync("onedrive", "account-a", {})
    assert result.complete and result.full_snapshot
    assert session.calls[1][1] == next_url
    assert [item["id"] for item in result] == ["one", "two"]


@pytest.mark.parametrize("bad_item", [
    {"id": "invalid", "deleted": None},
    {"id": "invalid", "deleted": True},
    {"id": "invalid", "size": float("nan")},
])
def test_invalid_page_tail_cannot_publish_any_entries_from_that_page(tmp_path, bad_item):
    session = Session(
        page("onedrive", [{"id": "first", "name": "valid-page"}], "next"),
        page("onedrive", [{"id": "first", "name": "rejected-page"}, {"id": "bad-page-only"}, bad_item]),
    )
    plugin = plugin_for(tmp_path, session, provider="onedrive")
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "onedrive", "account": "account-a"})
    assert result.status is ToolRunStatus.PARTIAL
    assert ids(plugin.catalog, "onedrive") == {"old", "first"}
    first = next(item for item in plugin.catalog.list("onedrive", "account-a") if item["id"] == "first")
    assert first["name"] == "valid-page"
    assert result.evidence[0].data["pages_fetched"] == 1
    assert result.evidence[0].data["next_cursor"]


def test_empty_nonterminal_page_is_followed_instead_of_clearing_catalog(tmp_path):
    session = Session(page("google_drive", [], "next"), page("google_drive", [{"id": "tail"}]))
    plugin = plugin_for(tmp_path, session)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.succeeded and len(session.calls) == 2
    assert ids(plugin.catalog) == {"tail"}


def test_scan_deadline_stops_before_next_page_and_keeps_valid_partial_results(tmp_path, monkeypatch):
    clock = {"now": 0.0, "checks": 0}
    monkeypatch.setattr("core.remote_runtime.time.monotonic", lambda: clock["now"])

    def budget_expires_between_pages():
        clock["checks"] += 1
        if clock["checks"] == 3:
            clock["now"] = 61.0

    session = Session(page("google_drive", [{"id": "first"}], "next"))
    result = ProviderApi(OAuth(), session).sync(
        "google_drive", "account-a", {}, check_cancelled=budget_expires_between_pages,
    )
    assert len(session.calls) == 1 and result.error == "page_timeout"
    assert result.next_cursor and not result.complete
    plugin = plugin_for(tmp_path, Session())
    plugin.api = type("PartialApi", (), {"sync": lambda *_args, **_kwargs: result})()
    delivered = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert delivered.status is ToolRunStatus.PARTIAL and ids(plugin.catalog) == {"old", "first"}


def test_turn_cancel_during_final_response_does_not_publish_or_claim_completion(tmp_path):
    from core.turn_context import TurnExecutionContext, bind_turn_context

    context = TurnExecutionContext("cloud-turn", "cloud-session")

    def cancelled_response():
        context.cancel()
        return page("google_drive", [{"id": "new"}])

    plugin = plugin_for(tmp_path, Session(cancelled_response))
    with bind_turn_context(context):
        result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.status is ToolRunStatus.CANCELLED
    assert ids(plugin.catalog) == {"old"}
    assert len(plugin.api.session.calls) == 1


def test_sql_insert_failure_rolls_back_snapshot_deletion_and_revision(tmp_path):
    plugin = plugin_for(tmp_path, Session(page("google_drive", [{"id": "cannot-store"}])))
    revision = plugin.catalog.revision("google_drive", "account-a")
    with plugin.catalog._session() as conn:
        conn.execute("""CREATE TRIGGER reject_test_insert BEFORE INSERT ON remote_catalog
                        WHEN NEW.item_id = 'cannot-store'
                        BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.status is ToolRunStatus.FAILED
    assert ids(plugin.catalog) == {"old"}
    assert plugin.catalog.revision("google_drive", "account-a") == revision
    assert result.evidence[0].data["catalog_mode"] == "unchanged"


def test_other_provider_update_does_not_invalidate_current_scope(tmp_path):
    plugin = plugin_for(tmp_path, Session())

    def update_other_provider():
        plugin.catalog.replace("notion", "account-a", [{"id": "other-provider"}])
        return page("google_drive", [{"id": "current-provider"}])

    plugin.api.session = Session(update_other_provider)
    result = plugin.execute_tool("cloud_sync_catalog", {"provider": "google_drive", "account": "account-a"})
    assert result.succeeded
    assert ids(plugin.catalog) == {"current-provider"}
    assert ids(plugin.catalog, "notion") == {"other-provider"}
