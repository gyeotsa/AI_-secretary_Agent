"""Independent Drive protocol/body/RAG oracles using isolated stores and fake HTTP."""
import hashlib
import json
from types import SimpleNamespace

import pytest
import requests

from core.cloud_content import CloudContentRuntime
from core.plugin import ToolCancelledError
from core.rag import VectorRAGManager


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        self.status_code, self.closed = status, False
        self.headers = {"Content-Length": str(len(self.body))} if headers is None else headers

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **options):
        self.calls.append((method, url, options))
        assert method == "GET" and url.startswith("https://www.googleapis.com/drive/v3/files/")
        assert options["allow_redirects"] is False and options["stream"] is True and options["timeout"] == 15
        assert self.responses, "Unexpected remote call"
        response = self.responses.pop(0)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response


class OAuth:
    def __init__(self):
        self.calls = []

    def access_token(self, provider, account):
        self.calls.append((provider, account))
        assert provider == "google"
        return "fixture-only"


def metadata(file_id="a", version="1", mime="text/plain", **fields):
    return Response({"id": file_id, "name": "fixture", "version": version,
                     "mimeType": mime, "modifiedTime": "2026-10-04T00:00:00Z", "trashed": False,
                     "capabilities": {"canDownload": True}, **fields})


def text_rag(tmp_path):
    # Exercise real chunk storage and retrieval without vector models or user data initialization.
    rag = object.__new__(VectorRAGManager)
    rag.documents, rag.namespace, rag.use_vector_rag = {}, "global", False
    rag.rag_file, rag.usage_tracker = str(tmp_path / "rag.json"), None
    return rag


def runtime(tmp_path, *responses, rag=None):
    oauth, session = OAuth(), Session(*responses)
    return CloudContentRuntime(SimpleNamespace(oauth=oauth, session=session), rag or text_rag(tmp_path))


def sync(value, ids=None, account="account"):
    return value.sync("google_drive", account, ["a"] if ids is None else ids)


def document(value, account="account", file_id="a"):
    return value._document(value.namespace("google_drive", account), file_id)


def test_real_text_body_hash_source_identity_and_persistence_not_metadata_snippet(tmp_path):
    body = "원격 회의록: 금요일에 배포를 진행합니다."
    replies = [metadata(), Response(body.encode()), metadata()]
    value = runtime(tmp_path, *replies)
    result = sync(value)
    assert result["complete"]
    stored = document(value)
    expected = hashlib.sha256(body.encode()).hexdigest()
    assert stored["chunks"] == [body] and stored["content_sha256"] == expected
    assert stored["metadata"]["remote_id"] == "a" and stored["metadata"]["account"] == "account"
    assert stored["source"] == "https://drive.google.com/file/d/a/view"
    assert stored["metadata"]["raw_sha256"] == expected
    assert stored["metadata"]["content_role"] == "untrusted_source_data"
    saved = json.loads((tmp_path / "rag.json").read_text(encoding="utf-8"))
    assert saved[value.namespace("google_drive", "account") + "::google-drive-a"]["content_sha256"] == expected
    assert value.api.session.calls[1][2]["params"] == {"alt": "media", "supportsAllDrives": "true"}
    assert all(reply.closed for reply in replies)
    assert value.api.oauth.calls == [("google", "account")] * 3


def test_google_doc_uses_actual_text_export_not_alt_media(tmp_path):
    value_mime = CloudContentRuntime.GOOGLE_DOC
    value = runtime(tmp_path, metadata(mime=value_mime),
                    Response("구글 문서 본문입니다.".encode()), metadata(mime=value_mime))
    assert sync(value)["complete"]
    call = value.api.session.calls[1]
    assert call[1].endswith("/a/export") and call[2]["params"] == {"mimeType": "text/plain"}
    assert document(value)["source"] == "https://docs.google.com/document/d/a/edit"


@pytest.mark.parametrize("mime", ["text/markdown", "text/x-markdown", "text/csv"])
def test_declared_supported_utf8_text_formats_have_real_body_path(tmp_path, mime):
    value = runtime(tmp_path, metadata(mime=mime), Response(b"target body"), metadata(mime=mime))
    assert sync(value)["complete"] and document(value)["chunks"] == ["target body"]


def test_incremental_unchanged_version_skips_export_and_changed_version_replaces_chunks(tmp_path):
    value = runtime(tmp_path, metadata(), Response(b"old target"), metadata())
    sync(value)
    value.api.session.responses = [metadata()]
    unchanged = sync(value)
    assert unchanged["results"][0]["status"] == "unchanged" and len(value.api.session.calls) == 4
    value.api.session.responses = [metadata(version="2"), Response(b"new target"), metadata(version="2")]
    assert sync(value)["results"][0]["status"] == "indexed"
    assert document(value)["chunks"] == ["new target"]
    assert document(value)["metadata"]["remote_version"] == "2"
    assert len(value.rag.documents) == 1


@pytest.mark.parametrize("failure", [
    Response(status=404),
    Response({"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}, status=403),
    metadata(trashed=True),
    metadata(capabilities={"canDownload": False}),
])
def test_confirmed_unavailable_id_excludes_only_its_old_body(tmp_path, failure):
    value = runtime(tmp_path, metadata(), Response(b"old body"), metadata(),
                    metadata(file_id="b"), Response(b"keep body"), metadata(file_id="b"))
    sync(value, ["a", "b"])
    value.api.session.responses = [failure]
    result = sync(value)
    assert not result["complete"] and result["results"][0]["previous_body_excluded"]
    assert document(value) is None and document(value, file_id="b")["chunks"] == ["keep body"]
    assert failure.closed


@pytest.mark.parametrize("failure", [Response(status=500), Response(status=429), Response(status=401),
    Response({"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}, status=403), requests.Timeout("private error")])
def test_transient_or_auth_failure_preserves_old_cache_but_is_not_success(tmp_path, failure):
    value = runtime(tmp_path, metadata(), Response(b"old body"), metadata())
    sync(value)
    value.api.session.responses = [failure]
    result = sync(value)
    assert not result["complete"] and not result["results"][0]["previous_body_excluded"]
    assert document(value)["chunks"] == ["old body"]
    assert "private error" not in json.dumps(result)


@pytest.mark.parametrize("mime", ["application/pdf", "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation", "application/vnd.google-apps.shortcut",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"])
def test_unsupported_types_do_not_index_fake_metadata_or_download_body(tmp_path, mime):
    value = runtime(tmp_path, metadata(mime=mime))
    result = sync(value)
    assert result["results"][0]["reason"] == "unsupported_mime_type" and not result["complete"]
    assert not value.rag.documents and len(value.api.session.calls) == 1


def test_source_version_change_during_body_fetch_does_not_publish_old_content(tmp_path):
    value = runtime(tmp_path, metadata(), Response(b"target"), metadata(version="2"))
    result = sync(value)
    assert result["results"][0]["reason"] == "source_changed_during_fetch"
    assert not value.rag.documents


@pytest.mark.parametrize("body", [b"\xff\xfe\xfd", b"", b"binary\x00data"])
def test_invalid_utf8_empty_and_binary_bodies_are_not_rag_success(tmp_path, body):
    value = runtime(tmp_path, metadata(), Response(body))
    assert not sync(value)["complete"] and not value.rag.documents


def test_size_bound_and_incomplete_stream_close_response_without_indexing(tmp_path):
    response = Response(b"x" * 10, headers={"Content-Length": "20"})
    value = runtime(tmp_path, metadata(), response)
    result = sync(value)
    assert result["results"][0]["reason"] == "incomplete_body" and response.closed
    oversized = Response(b"x" * 10, headers={})
    value = runtime(tmp_path, metadata(), oversized)
    value.MAX_BYTES = 5
    assert sync(value)["results"][0]["reason"] == "body_too_large" and oversized.closed


def test_gzip_content_length_is_not_mistaken_for_decompressed_body_length(tmp_path):
    response = Response(b"target body", headers={"Content-Length": "5", "Content-Encoding": "gzip"})
    value = runtime(tmp_path, metadata(), response, metadata())
    assert sync(value)["complete"]


def test_remote_content_checksum_mismatch_is_not_accepted(tmp_path):
    value = runtime(tmp_path, metadata(md5Checksum="0" * 32), Response(b"target"))
    assert sync(value)["results"][0]["reason"] == "remote_checksum_mismatch"
    assert not value.rag.documents


@pytest.mark.parametrize("ids", [[], ["a", "a"], ["../escape"], ["https://evil.example"], ["a"] * 11, [123]])
def test_only_explicit_bounded_valid_ids_are_fetched(tmp_path, ids):
    value = runtime(tmp_path)
    with pytest.raises(ValueError):
        sync(value, ids)
    assert not value.api.session.calls


def test_namespace_is_exact_account_isolated_and_not_global(tmp_path):
    value = runtime(tmp_path)
    scopes = {value.namespace("google_drive", account) for account in ["a.b", "ab", "AB", "a-b"]}
    assert len(scopes) == 4 and "global" not in scopes
    with pytest.raises(ValueError):
        value.namespace("onedrive", "a")


def test_partial_batch_and_cancellation_preserve_completed_and_unrelated_documents(tmp_path):
    cancelled = False
    def interrupted():
        nonlocal cancelled
        cancelled = True
        return metadata(file_id="b")
    value = runtime(tmp_path, metadata(), Response(b"completed body"), metadata(), interrupted)
    def checkpoint():
        if cancelled:
            raise ToolCancelledError("fixture")
    result = value.sync("google_drive", "account", ["a", "b"], check_cancelled=checkpoint)
    assert result["cancelled"] and result["remaining_ids"] == ["b"]
    assert result["results"][0]["status"] == "indexed" and document(value)["chunks"] == ["completed body"]


def test_search_uses_existing_rag_chunks_and_rechecks_access_before_returning_evidence(tmp_path):
    value = runtime(tmp_path, metadata(), Response("회의록 target 원격 근거".encode()), metadata())
    sync(value)
    value.api.session.responses = [metadata()]
    found = value.search("google_drive", "account", "target")
    assert found["answerable"] and found["complete"]
    row = found["results"][0]
    assert row["content"] == "회의록 target 원격 근거" and row["remote_id"] == "a"
    assert row["content_sha256"] == document(value)["content_sha256"]
    assert row["citation"]["source"] == "https://drive.google.com/file/d/a/view"
    assert row["chunk_id"] and not found["answer_usage_verified"]
    assert value.search("google_drive", "other-account", "target")["results"] == []


def test_search_reindexes_changed_candidates_and_does_not_return_old_or_revoked_body(tmp_path):
    value = runtime(tmp_path, metadata(), Response(b"old target"), metadata())
    sync(value)
    value.api.session.responses = [metadata(version="2"), Response(b"new target"), metadata(version="2")]
    found = value.search("google_drive", "account", "target")
    assert found["results"][0]["content"] == "new target"
    value.api.session.responses = [Response(status=404)]
    missing = value.search("google_drive", "account", "target")
    assert not missing["complete"] and not missing["answerable"] and missing["results"] == []
    assert document(value) is None


def test_search_keeps_transiently_failed_cache_but_does_not_return_it_as_fresh_source(tmp_path):
    value = runtime(tmp_path, metadata(), Response(b"target cached"), metadata())
    sync(value)
    value.api.session.responses = [Response(status=500)]
    found = value.search("google_drive", "account", "target")
    assert not found["complete"] and not found["answerable"] and found["results"] == []
    assert document(value)["chunks"] == ["target cached"]


def test_cancel_after_rag_publication_reports_actual_interrupted_cache_not_rollback(tmp_path, monkeypatch):
    value = runtime(tmp_path, metadata(), Response(b"target published"), metadata())
    add = value.rag.add_text_document
    cancelled = False
    def publish_then_cancel(*args, **options):
        nonlocal cancelled
        add(*args, **options)
        cancelled = True
        raise ToolCancelledError("publication already started")
    monkeypatch.setattr(value.rag, "add_text_document", publish_then_cancel)
    def checkpoint():
        if cancelled:
            raise ToolCancelledError("fixture")
    result = value.sync("google_drive", "account", ["a"], check_cancelled=checkpoint)
    assert result["cancelled"] and not result["complete"]
    state = result["interrupted_file_state"]
    assert state["body_present"] and state["content_sha256"] == hashlib.sha256(b"target published").hexdigest()
    assert state["remote_version"] == "1" and document(value)["chunks"] == ["target published"]


def test_late_index_publication_is_reported_partial_with_the_actual_indexed_result(tmp_path, monkeypatch):
    import time
    value = runtime(tmp_path, metadata(), Response(b"target published"), metadata())
    add = value.rag.add_text_document
    def slow(*args, **options):
        add(*args, **options)
        time.sleep(0.03)
    monkeypatch.setattr(value.rag, "add_text_document", slow)
    value.TOTAL_SECONDS = 0.02
    result = sync(value)
    assert result["deadline_exceeded"] and not result["complete"]
    assert result["results"][0]["status"] == "indexed" and document(value)["chunks"] == ["target published"]


def test_failed_exclusion_is_explicit_and_not_falsely_reported_removed(tmp_path, monkeypatch):
    value = runtime(tmp_path, metadata(), Response(b"target cached"), metadata())
    sync(value)
    value.api.session.responses = [Response(status=404)]
    def fail_remove(*_args, **_options):
        raise OSError("fixture private message")
    monkeypatch.setattr(value.rag, "remove_text_document", fail_remove)
    result = sync(value)
    assert result["results"][0]["removal_error"] == "OSError"
    assert not result["results"][0]["previous_body_excluded"]
    assert document(value)["chunks"] == ["target cached"]


def test_global_cloud_document_is_not_returned_in_an_account_namespace(tmp_path):
    value = runtime(tmp_path)
    value.rag.add_text_document("target global", doc_id="global", namespace="global",
        metadata={"source_type": "cloud", "provider": "google_drive", "account": "account", "remote_id": "a"})
    assert value.search("google_drive", "account", "target")["results"] == []
    assert not value.api.session.calls


class FixtureVectors:
    """Chroma protocol oracle; it deliberately over-returns query candidates."""
    def __init__(self):
        self.rows, self.fail_delete = {}, False

    @staticmethod
    def matches(metadata, where):
        if "$and" in where:
            return all(FixtureVectors.matches(metadata, condition) for condition in where["$and"])
        return all(metadata.get(key) == expected for key, expected in where.items())

    def get(self, where):
        return {"ids": [key for key, (_, metadata) in self.rows.items() if self.matches(metadata, where)]}

    def add(self, ids, documents, metadatas):
        self.rows.update({key: (content, metadata) for key, content, metadata in zip(ids, documents, metadatas)})

    def delete(self, ids):
        if self.fail_delete:
            raise RuntimeError("fixture vector unavailable")
        for key in ids:
            self.rows.pop(key, None)

    def query(self, **_options):
        return {"documents": [[content for content, _ in self.rows.values()]],
                "metadatas": [[metadata for _, metadata in self.rows.values()]],
                "distances": [[0.1] * len(self.rows)]}


def test_existing_vector_rag_route_retains_provider_hash_and_source_citations(tmp_path):
    rag = text_rag(tmp_path)
    rag.use_vector_rag, rag.collection, rag.reranker = True, FixtureVectors(), None
    value = runtime(tmp_path, metadata(), Response(b"target vector body"), metadata(), rag=rag)
    assert sync(value)["complete"] and rag.collection.rows
    value.api.session.responses = [metadata()]
    row = value.search("google_drive", "account", "target")["results"][0]
    assert row["content"] == "target vector body" and row["remote_id"] == "a" and row["content_sha256"]
    assert "vector" in row["retrieval_channels"] and row["citation"]["source"].endswith("/a/view")


def test_orphan_vector_after_revocation_cleanup_failure_cannot_resurface_account_body(tmp_path):
    rag = text_rag(tmp_path)
    rag.use_vector_rag, rag.collection, rag.reranker = True, FixtureVectors(), None
    value = runtime(tmp_path, metadata(), Response(b"target vector body"), metadata(), rag=rag)
    sync(value)
    rag.collection.fail_delete = True
    value.api.session.responses = [Response(status=404)]
    found = value.search("google_drive", "account", "target")
    assert not found["results"] and not found["answerable"]
    assert rag.collection.rows and document(value) is None
    # The leftover vector lacks an authoritative document-catalog account binding.
    assert value.search("google_drive", "account", "target")["results"] == []
