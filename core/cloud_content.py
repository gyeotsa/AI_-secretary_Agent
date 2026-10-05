"""Explicit Google Drive text bodies -> account-scoped existing RAG.

No background scan, metadata-only indexing, model call, or extra persistence.
Official protocols: developers.google.com/workspace/drive/api/reference/rest/v3/files/{get,export}.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager

from core.local_inference import local_inference
from core.plugin import ToolCancelledError
from core.remote_runtime import OAuthCoordinator, ProviderApi, SecureTokenVault


class CloudContentError(RuntimeError):
    def __init__(self, reason, *, exclude=False):
        super().__init__(reason)
        self.reason, self.exclude = reason, exclude


class CloudContentRuntime:
    PROVIDER = "google_drive"
    GOOGLE_DOC = "application/vnd.google-apps.document"
    TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown", "text/csv"}
    MAX_BYTES = 2 * 1024 * 1024
    TOTAL_SECONDS = 60.0
    _lock = threading.RLock()

    def __init__(self, api=None, rag=None):
        self._api, self._rag = api, rag

    @property
    def api(self):
        if self._api is None:
            self._api = ProviderApi(OAuthCoordinator())
        return self._api

    @property
    def rag(self):
        if self._rag is None:
            from core.rag import get_rag_manager
            self._rag = get_rag_manager()
        return self._rag

    @classmethod
    def namespace(cls, provider, account):
        if provider != cls.PROVIDER:
            raise ValueError("unsupported_cloud_content_provider")
        SecureTokenVault._scope(provider, account)
        return "cloud:google_drive:" + hashlib.sha256(account.encode("utf-8")).hexdigest()

    @staticmethod
    def _file_ids(file_ids):
        if (not isinstance(file_ids, list) or not 1 <= len(file_ids) <= 10
                or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value)
                       for value in file_ids) or len(set(file_ids)) != len(file_ids)):
            raise ValueError("explicit_unique_drive_file_ids_required")
        return file_ids

    @contextmanager
    def _lease(self, checkpoint):
        # ponytail: one local cloud-index writer; use account locks if throughput matters.
        while not self._lock.acquire(timeout=0.05):
            checkpoint()
        try:
            checkpoint()
            yield
        finally:
            self._lock.release()

    def _get(self, account, file_id, params, checkpoint, *, export=False, max_bytes=None):
        """Fixed provider URL; don't follow authenticated redirects or load unbounded bodies."""
        checkpoint()
        token = self.api.oauth.access_token("google", account)
        checkpoint()
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}" + ("/export" if export else "")
        response = self.api.session.request(
            "GET", url, headers={"Authorization": f"Bearer {token}"}, params=params,
            timeout=15, allow_redirects=False, stream=True,
        )
        try:
            checkpoint()
            status = response.status_code
            if status == 404:
                raise CloudContentError("not_found", exclude=True)
            if status == 403:
                # Rate-limit 403s are transient, not proof that the document was revoked.
                error_data = self._read_bytes(response, 65536, checkpoint)
                try:
                    reasons = {item.get("reason") for item in json.loads(error_data).get("error", {}).get("errors", [])}
                except (ValueError, TypeError, AttributeError):
                    reasons = set()
                transient = {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"}
                raise CloudContentError("rate_limited" if reasons and reasons <= transient else "access_denied",
                                        exclude=not bool(reasons and reasons <= transient))
            if status != 200:
                raise CloudContentError(f"http_{status}")
            return self._read_bytes(response, max_bytes or self.MAX_BYTES, checkpoint)
        finally:
            response.close()

    @staticmethod
    def _read_bytes(response, limit, checkpoint):
        declared = response.headers.get("Content-Length")
        if declared is not None and (not declared.isdecimal() or int(declared) > limit):
            raise CloudContentError("body_too_large")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=16384):
            checkpoint()
            if not isinstance(chunk, bytes) or len(content) + len(chunk) > limit:
                raise CloudContentError("body_too_large")
            content.extend(chunk)
        checkpoint()
        if (declared is not None and response.headers.get("Content-Encoding", "identity") == "identity"
                and len(content) != int(declared)):
            raise CloudContentError("incomplete_body")
        return bytes(content)

    def _metadata(self, account, file_id, checkpoint):
        content = self._get(account, file_id, {
            "fields": "id,name,mimeType,version,modifiedTime,trashed,capabilities(canDownload),md5Checksum",
            "supportsAllDrives": "true",
        }, checkpoint, max_bytes=65536)
        metadata = json.loads(content)
        if (not isinstance(metadata, dict) or metadata.get("id") != file_id
                or not isinstance(metadata.get("mimeType"), str)
                or not isinstance(metadata.get("version"), str) or not metadata["version"].isdecimal()
                or len(metadata["version"]) > 30 or type(metadata.get("trashed")) is not bool
                or not isinstance(metadata.get("name"), str) or len(metadata["name"]) > 1024):
            raise CloudContentError("invalid_source_metadata")
        if metadata["trashed"]:
            raise CloudContentError("trashed", exclude=True)
        if metadata.get("capabilities", {}).get("canDownload") is False:
            raise CloudContentError("download_denied", exclude=True)
        if metadata["mimeType"] not in self.TEXT_TYPES | {self.GOOGLE_DOC}:
            raise CloudContentError("unsupported_mime_type", exclude=True)
        return metadata

    def _document(self, namespace, file_id):
        return next((document for document in self.rag._documents_snapshot()
                     if document.get("namespace") == namespace
                     and document.get("doc_id") == "google-drive-" + file_id), None)

    @staticmethod
    def _revision(metadata):
        return tuple(metadata.get(field) for field in ("id", "version", "mimeType", "name", "modifiedTime"))

    def _sync_one(self, account, file_id, namespace, checkpoint):
        metadata = self._metadata(account, file_id, checkpoint)
        doc_id = "google-drive-" + file_id
        old = self._document(namespace, file_id)
        if old and old.get("metadata", {}).get("remote_revision") == list(self._revision(metadata)):
            return {"remote_id": file_id, "doc_id": doc_id, "status": "unchanged",
                    "content_sha256": old["content_sha256"], "source_url": old["source"],
                    "remote_version": metadata["version"]}
        is_doc = metadata["mimeType"] == self.GOOGLE_DOC
        raw = self._get(account, file_id, {"mimeType": "text/plain"} if is_doc else {
                            "alt": "media", "supportsAllDrives": "true"},
                        checkpoint, export=is_doc)
        if not is_doc and metadata.get("md5Checksum"):
            if hashlib.md5(raw).hexdigest() != metadata["md5Checksum"]:
                raise CloudContentError("remote_checksum_mismatch")
        text = raw.decode("utf-8-sig").strip()
        if not text or "\x00" in text:
            raise CloudContentError("empty_or_binary_body")
        after = self._metadata(account, file_id, checkpoint)
        if self._revision(after) != self._revision(metadata):
            raise CloudContentError("source_changed_during_fetch")
        checkpoint()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source = (f"https://docs.google.com/document/d/{file_id}/edit" if is_doc
                  else f"https://drive.google.com/file/d/{file_id}/view")
        with local_inference(check=checkpoint, timeout=self.TOTAL_SECONDS):
            self.rag.add_text_document(text, doc_id=doc_id, namespace=namespace, source_uri=source, metadata={
                "source_type": "cloud", "provider": self.PROVIDER, "account": account,
                "remote_id": file_id, "remote_version": metadata["version"],
                "remote_revision": list(self._revision(metadata)), "mime_type": metadata["mimeType"],
                "title": metadata["name"], "content_sha256": digest,
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "content_role": "untrusted_source_data",
                "verified_at": time.time(),
            })
        stored = self._document(namespace, file_id)
        if not stored or stored.get("content_sha256") != digest:
            raise CloudContentError("index_readback_mismatch")
        return {"remote_id": file_id, "doc_id": doc_id, "status": "indexed",
                "content_sha256": digest, "source_url": source, "remote_version": metadata["version"]}

    def sync(self, provider, account, file_ids, *, check_cancelled=None):
        namespace = self.namespace(provider, account)
        self._file_ids(file_ids)
        deadline = time.perf_counter() + self.TOTAL_SECONDS

        def checkpoint():
            if check_cancelled:
                check_cancelled()
            if time.perf_counter() >= deadline:
                raise TimeoutError("cloud_content_deadline")

        results = []

        def interrupted(file_id):
            # RAG publication may have started before cancellation; don't promise rollback.
            current = self._document(namespace, file_id)
            return {"provider": provider, "account": account, "namespace": namespace,
                    "results": results, "complete": False, "cancelled": True,
                    "remaining_ids": file_ids[len(results):], "scope": "explicit_file_ids",
                    "interrupted_file_state": {"remote_id": file_id, "body_present": current is not None,
                                               "content_sha256": (current or {}).get("content_sha256", ""),
                                               "remote_version": (current or {}).get("metadata", {}).get("remote_version", "")}}

        with self._lease(checkpoint):
            for file_id in file_ids:
                try:
                    checkpoint()
                    results.append(self._sync_one(account, file_id, namespace, checkpoint))
                except ToolCancelledError:
                    return interrupted(file_id)
                except Exception as exc:
                    if check_cancelled:
                        try:
                            check_cancelled()
                        except ToolCancelledError:
                            return interrupted(file_id)
                    excluded, removal_error = False, ""
                    if isinstance(exc, CloudContentError) and exc.exclude:
                        try:
                            checkpoint()
                            excluded = self.rag.remove_text_document("google-drive-" + file_id, namespace=namespace)
                        except ToolCancelledError:
                            return interrupted(file_id)
                        except Exception as removal:
                            removal_error = type(removal).__name__
                    results.append({"remote_id": file_id, "status": "excluded" if excluded else "unavailable",
                                    "reason": exc.reason if isinstance(exc, CloudContentError) else type(exc).__name__,
                                    "previous_body_excluded": excluded, "removal_error": removal_error})
        late = time.perf_counter() >= deadline
        return {"provider": provider, "account": account, "namespace": namespace,
                "results": results, "complete": not late and all(row["status"] in {"indexed", "unchanged"} for row in results),
                "deadline_exceeded": late,
                "cancelled": False, "remaining_ids": [], "scope": "explicit_file_ids"}

    def search(self, provider, account, query, *, top_k=3, check_cancelled=None):
        namespace = self.namespace(provider, account)
        if (not isinstance(query, str) or not query.strip() or len(query) > 4096
                or isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10):
            raise ValueError("invalid_cloud_search")
        filters = {"source_type": "cloud", "provider": provider, "account": account}
        def checkpoint():
            if check_cancelled:
                check_cancelled()

        with local_inference(check=checkpoint, timeout=self.TOTAL_SECONDS):
            candidates = self.rag.search_docs(query, top_k=top_k, metadata_filter=filters, namespace=namespace)
        candidates = [row for row in candidates if row.get("namespace") == namespace]
        ids = list(dict.fromkeys(row.get("remote_id") for row in candidates if row.get("remote_id")))
        if not ids:
            return {"provider": provider, "account": account, "namespace": namespace, "results": [],
                    "complete": True, "answerable": False, "scope": "bounded_indexed_candidates",
                    "answer_usage_verified": False}
        refresh = self.sync(provider, account, ids, check_cancelled=check_cancelled)
        allowed = {row["remote_id"] for row in refresh["results"] if row["status"] in {"indexed", "unchanged"}}
        # Transient errors keep cached storage, but stale/unconfirmed bodies aren't returned as fresh evidence.
        found = []
        if not refresh["cancelled"]:
            with local_inference(check=checkpoint, timeout=self.TOTAL_SECONDS):
                found = self.rag.search_docs(query, top_k=top_k, metadata_filter=filters, namespace=namespace)
            found = [row for row in found if row.get("namespace") == namespace and row.get("remote_id") in allowed]
        return {"provider": provider, "account": account, "namespace": namespace, "results": found,
                "complete": refresh["complete"], "cancelled": refresh["cancelled"],
                "refresh": refresh["results"], "answerable": bool(found),
                "scope": "bounded_indexed_candidates", "answer_usage_verified": False,
                "content_role": "untrusted_source_data"}
