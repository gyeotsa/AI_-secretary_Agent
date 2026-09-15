"""Cloud, OAuth, and communication tools with explicit draft/apply boundaries."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from core.plugin import BasePlugin, PluginStateProbe, ToolCancelledError, ToolSchema
from core.remote_runtime import (CatalogSyncResult, OAuthCoordinator, ProviderApi, RemoteActionStore,
                                 RemoteApplyReceipt, RemoteApplyRejected,
                                 RemoteApplyUncertain, RemoteCatalogStore,
                                 RemoteRuntimeError)
from core.tool_result import Evidence, ToolRunResult, ToolRunStatus


class CloudCommunicationPlugin(BasePlugin):
    OPERATIONS = ["gmail_send", "google_calendar_create", "outlook_send",
                  "outlook_calendar_create", "slack_send", "teams_send"]

    def __init__(self):
        super().__init__()
        self.name = "cloud_communication"
        self.description = "OAuth, cloud catalog synchronization, and approved external messaging"
        self.auth_required = True
        self.auth_type = "provider-specific OAuth/API token"
        self.oauth = OAuthCoordinator()
        self.actions = RemoteActionStore()
        self.catalog = RemoteCatalogStore()
        self.api = ProviderApi(self.oauth)

    def probe_connection(self) -> PluginStateProbe:
        return PluginStateProbe("unchecked", "클라우드 제공자와 계정을 선택한 실제 연결 검증이 필요합니다.")

    def probe_authentication(self) -> PluginStateProbe:
        return PluginStateProbe("unchecked", "제공자·계정별 인증이 필요합니다. 토큰 저장 여부만으로 실제 인증을 보장하지 않습니다.")

    def diagnose(self) -> List[str]:
        issues = super().diagnose()
        for variable in ("GOOGLE_OAUTH_CLIENT_ID", "MICROSOFT_OAUTH_CLIENT_ID",
                         "SLACK_BOT_TOKEN", "NOTION_API_TOKEN"):
            if not os.getenv(variable):
                issues.append(f"선택 연동 미설정: {variable}")
        return issues

    def get_tools(self) -> List[ToolSchema]:
        provider_account = {
            "provider": {"type": "string"},
            "account": {"type": "string", "minLength": 1},
        }
        return [
            ToolSchema("oauth_begin", "Google 또는 Microsoft OAuth PKCE 인증을 시작합니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["google", "microsoft"]},
                    "redirect_uri": {"type": "string", "minLength": 1}},
                "required": ["provider", "account", "redirect_uri"], "additionalProperties": False,
            }, ["cloud_account"], side_effect="execute", verification_required=False),
            ToolSchema("oauth_complete", "OAuth callback code를 DPAPI 암호화 저장소에 보관합니다.", {
                "type": "object", "properties": {"state": {"type": "string"}, "code": {"type": "string"}},
                "required": ["state", "code"], "additionalProperties": False,
            }, ["cloud_account"], side_effect="change"),
            ToolSchema("oauth_status", "비밀값을 노출하지 않고 OAuth 연결 상태만 확인합니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["google", "microsoft"]}},
                "required": ["provider", "account"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="read", verification_required=False),
            ToolSchema("remote_create_draft", "외부 반영 없이 승인 대기 초안을 로컬 원장에 만듭니다.", {
                "type": "object", "properties": {**provider_account,
                    "operation": {"enum": self.OPERATIONS}, "payload": {"type": "object"}},
                "required": ["provider", "account", "operation", "payload"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="change"),
            ToolSchema("remote_apply_draft", "승인된 초안을 외부에 반영하고 원격 ID를 재조회해 검증합니다.", {
                "type": "object", "properties": {"action_id": {"type": "string", "minLength": 1}},
                "required": ["action_id"], "additionalProperties": False,
            }, ["external_send"], side_effect="external_send"),
            ToolSchema("cloud_sync_catalog", "Drive/OneDrive/Notion 메타데이터를 제한된 페이지 수로 조회합니다. 부분/재개/필터 조회는 기존 항목을 보존하며 본문이나 RAG를 동기화하지 않습니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["google_drive", "onedrive", "notion"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                              "description": "페이지 크기(기본 50). 전체 항목 제한이 아닙니다."},
                    "max_pages": {"type": "integer", "minimum": 1, "maximum": 10,
                                  "description": "한 호출의 페이지 예산(기본 5)."},
                    "cursor": {"type": "string", "maxLength": 65536,
                               "description": "이전 결과의 next_cursor를 그대로 전달합니다. 제공자/계정/query를 바꾸지 않습니다."},
                    "query": {"type": "string", "maxLength": 4096,
                              "description": "선택적 Drive q 표현식 또는 Notion 제목 검색. OneDrive는 지원하지 않습니다."}},
                "required": ["provider", "account"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="change", timeout_seconds=90, cancellable=True, max_retries=0),
            ToolSchema("communication_read_summary", "Slack 또는 Teams 메시지를 조회하고 간결한 근거 요약을 만듭니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["slack", "teams"]}, "channel": {"type": "string"},
                    "team_id": {"type": "string"}, "channel_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "required": ["provider", "account"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="read"),
            ToolSchema("calendar_read_range", "Google 또는 Microsoft 캘린더의 지정 기간 일정을 실시간 조회합니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["google", "microsoft"]},
                    "start": {"type": "string", "description": "ISO 8601 시작 시각"},
                    "end": {"type": "string", "description": "ISO 8601 종료 시각"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "required": ["provider", "account"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="read"),
        ]

    @staticmethod
    def _success(tool: str, raw: str, kind: str, data: Dict[str, Any]) -> ToolRunResult:
        return ToolRunResult.successful(tool_name=tool, raw_output=raw,
                                        evidence=[Evidence(kind, raw, data)])

    @staticmethod
    def _uncertain_apply_result(action, message: str) -> ToolRunResult:
        detail = {
            "action_id": action.action_id,
            "operation": action.operation,
            "status": action.status,
            "remote_id": action.remote_id,
            "verified": False,
            "retry_allowed": False,
            "error": str(message),
        }
        return ToolRunResult.unverified(
            tool_name="remote_apply_draft",
            raw_output=(
                "원격 작업의 최종 결과를 확인하지 못했습니다. 중복 전송을 막기 위해 자동 재시도하지 않습니다. "
                "제공자 화면에서 결과를 확인한 뒤 새 작업을 명시적으로 만드세요."
            ),
            evidence=[Evidence(
                "remote_apply_uncertain",
                "원격 부작용 가능성이 있어 같은 작업 ID의 재전송을 차단했습니다.",
                detail,
            )],
        )

    def _sync_catalog(self, data: Dict[str, Any]) -> ToolRunResult:
        from core.turn_context import check_turn_cancelled

        context = self.get_execution_context()

        def checkpoint():
            check_turn_cancelled()
            if context is not None:
                context.raise_if_cancelled()

        provider, account = data["provider"], data["account"]
        revision = self.catalog.revision(provider, account)
        result = self.api.sync(provider, account, data, check_cancelled=checkpoint)
        if not isinstance(result, CatalogSyncResult):
            raise RemoteRuntimeError("페이지 완료 증거가 없는 목록은 전체 동기화 결과로 저장하지 않습니다.")
        detail = result.metadata()
        detail.update({"count": 0, "remote_ids": [], "catalog_mode": "unchanged"})
        try:
            checkpoint()
            if result.cancelled:
                raise ToolCancelledError("카탈로그 조회가 취소되었습니다.")
            ids = self.catalog.apply_sync(provider, account, result,
                                          expected_revision=revision, check_cancelled=checkpoint)
        except ToolCancelledError:
            return ToolRunResult.cancelled(
                tool_name="cloud_sync_catalog", message="카탈로그 조회를 취소했습니다. 기존 목록은 변경하지 않았습니다.",
                evidence=[Evidence("cloud_catalog_partial", "취소로 저장하지 않은 조회 결과입니다.", detail)],
            )
        except Exception as exc:
            # In-flight scans cannot overwrite a newer catalog, and failed
            # transactions retain the complete old catalog.
            detail["store_error"] = type(exc).__name__
            return ToolRunResult.failed(
                tool_name="cloud_sync_catalog", error="카탈로그 저장을 확정하지 못해 기존 목록을 보존했습니다.",
                evidence=[Evidence("cloud_catalog_partial", "조회 결과를 저장하지 않았습니다.", detail)],
            )
        detail.update({"count": len(ids), "remote_ids": ids,
                       "catalog_mode": "replaced" if result.full_snapshot else "merged",
                       "deletions_deferred": bool(result.deleted_ids) and not result.full_snapshot})
        if result.full_snapshot:
            return self._success("cloud_sync_catalog",
                                 f"접근 가능한 메타데이터 {len(ids)}개를 끝 페이지까지 조회해 카탈로그를 갱신했습니다. 본문/RAG는 포함하지 않습니다.",
                                 "cloud_catalog", detail)
        raw = (f"부분 조회 결과 {len(ids)}개를 병합하고 기존 항목은 보존했습니다. "
               "전체 카탈로그 동기화 완료는 아닙니다. 본문/RAG는 포함하지 않습니다.")
        if result.next_cursor:
            raw += " 같은 제공자·계정·query와 next_cursor로 다음 페이지를 조회할 수 있습니다."
        if result.resumed and result.complete:
            raw += " 재개 조회의 끝에 도달했지만 이전 페이지를 합친 전체 스냅샷은 검증하지 않았습니다."
        return ToolRunResult(
            tool_name="cloud_sync_catalog",
            status=ToolRunStatus.FAILED if result.error and not ids else ToolRunStatus.PARTIAL,
            raw_output=raw, error=result.error or None,
            evidence=[Evidence("cloud_catalog_partial", raw, detail)],
        )

    def execute_tool(self, tool_name: str, data: Dict[str, Any]):
        try:
            if tool_name == "oauth_begin":
                result = self.oauth.begin(data["provider"], data["account"], data["redirect_uri"])
                return self._success(tool_name, "OAuth 인증 URL을 생성했습니다.", "oauth_pkce", result)
            if tool_name == "oauth_complete":
                result = self.oauth.complete(data["state"], data["code"])
                return self._success(tool_name, "OAuth 토큰을 암호화 저장했습니다.", "oauth_token_vault", result)
            if tool_name == "oauth_status":
                result = self.oauth.status(data["provider"], data["account"])
                return self._success(tool_name, "OAuth 연결 상태를 확인했습니다.", "oauth_status", result)
            if tool_name == "remote_create_draft":
                action = self.actions.create(data["provider"], data["account"], data["operation"], data["payload"])
                detail = {"action_id": action.action_id, "operation": action.operation, "status": action.status}
                return self._success(tool_name, "외부 반영 전 승인 대기 초안을 만들었습니다.", "remote_draft", detail)
            if tool_name == "remote_apply_draft":
                action = self.actions.get(data["action_id"])
                if action and action.status in {"applying", "uncertain"}:
                    return self._uncertain_apply_result(
                        action, action.last_error or "이전 적용 시도의 결과가 확정되지 않았습니다."
                    )
                if not action or action.status != "draft":
                    raise RemoteRuntimeError("초안이 없거나 이미 적용된 작업입니다.")
                claimed = self.actions.claim_for_apply(action.action_id)
                try:
                    receipt = self.api.apply(claimed)
                except RemoteApplyRejected as exc:
                    rejected = self.actions.release_rejected(claimed.action_id, str(exc))
                    return ToolRunResult.failed(
                        tool_name=tool_name,
                        error=str(exc),
                        evidence=[Evidence(
                            "remote_apply_rejected",
                            "원격 변경 전에 요청이 거절되어 같은 초안을 수정 후 다시 시도할 수 있습니다.",
                            {"action_id": rejected.action_id, "status": rejected.status,
                             "verified": False, "retry_allowed": True},
                        )],
                    )
                except RemoteApplyUncertain as exc:
                    uncertain = self.actions.mark_uncertain(
                        claimed.action_id, str(exc), exc.remote_id
                    )
                    return self._uncertain_apply_result(uncertain, str(exc))
                except Exception as exc:
                    uncertain = self.actions.mark_uncertain(claimed.action_id, str(exc))
                    return self._uncertain_apply_result(uncertain, str(exc))
                if not isinstance(receipt, RemoteApplyReceipt) or not receipt.verification:
                    uncertain = self.actions.mark_uncertain(
                        claimed.action_id,
                        "Provider API가 의미 검증 증거 없이 원격 ID만 반환했습니다.",
                        str(receipt or ""),
                    )
                    return self._uncertain_apply_result(uncertain, uncertain.last_error)
                try:
                    applied = self.actions.mark_applied(claimed.action_id, receipt.remote_id)
                except Exception as exc:
                    try:
                        uncertain = self.actions.mark_uncertain(
                            claimed.action_id,
                            f"원격 검증 후 로컬 원장 확정에 실패했습니다: {exc}",
                            receipt.remote_id,
                        )
                    except Exception:
                        uncertain = self.actions.get(claimed.action_id) or claimed
                    return self._uncertain_apply_result(uncertain, str(exc))
                detail = {"action_id": action.action_id, "remote_id": applied.remote_id,
                          "status": applied.status, "verified": True,
                          "retry_allowed": False,
                          "read_back": dict(receipt.verification)}
                return self._success(tool_name, "승인된 작업을 외부에 반영하고 원격 ID를 검증했습니다.",
                                     "remote_content_readback", detail)
            if tool_name == "cloud_sync_catalog":
                return self._sync_catalog(data)
            if tool_name == "communication_read_summary":
                messages = self.api.read_messages(data["provider"], data["account"], data)
                normalized = []
                for message in messages:
                    text = message.get("text") or message.get("body", {}).get("content", "")
                    normalized.append({"remote_id": str(message.get("ts") or message.get("id") or ""),
                                       "author": str(message.get("user") or message.get("from", {}).get("user", {}).get("displayName", "")),
                                       "snippet": str(text)[:160]})
                detail = {"provider": data["provider"], "count": len(normalized), "messages": normalized}
                return self._success(tool_name, f"메시지 {len(normalized)}개를 조회해 요약했습니다.",
                                     "communication_summary", detail)
            if tool_name == "calendar_read_range":
                now = datetime.now().astimezone()
                start = str(data.get("start") or now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).isoformat())
                end = str(data.get("end") or (
                    now.replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1)
                ).isoformat())
                events = self.api.read_calendar(
                    data["provider"], data["account"], start, end,
                    limit=int(data.get("limit", 50)),
                )
                normalized = []
                for event in events:
                    start_value = event.get("start", {})
                    end_value = event.get("end", {})
                    normalized.append({
                        "remote_id": str(event.get("id", "")),
                        "title": str(event.get("summary") or event.get("subject") or "(제목 없음)"),
                        "start": str(start_value.get("dateTime") or start_value.get("date") or ""),
                        "end": str(end_value.get("dateTime") or end_value.get("date") or ""),
                        "location": str(
                            event.get("location", {}).get("displayName", "")
                            if isinstance(event.get("location"), dict)
                            else event.get("location", "")
                        ),
                        "web_link": str(event.get("htmlLink") or event.get("webLink") or ""),
                    })
                detail = {
                    "provider": data["provider"], "account": data["account"],
                    "start": start, "end": end, "count": len(normalized),
                    "events": normalized,
                    "retrieved_at": datetime.now(timezone.utc).astimezone().isoformat(),
                }
                return self._success(
                    tool_name, f"캘린더 일정 {len(normalized)}개를 실시간 조회했습니다.",
                    "calendar_events", detail,
                )
            raise ValueError(f"지원하지 않는 도구: {tool_name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
