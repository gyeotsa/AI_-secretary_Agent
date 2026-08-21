"""P12 administration tools. Secrets can be written/deleted but are never returned."""
from pathlib import Path
from core.plugin import BasePlugin, ToolSchema
from core.permission import get_permission_manager
from core.productization import DiagnosticReporter, METRICS, SafeModeManager, WindowsCredentialVault
from core.tool_result import Evidence, ToolRunResult


class ProductizationPlugin(BasePlugin):
    def __init__(self):
        super().__init__(); self.name = "productization"; self.description = "Security and diagnostics"

    def get_tools(self):
        obj = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            ToolSchema("security_list_scoped_permissions", "범위별 권한 목록", obj, ["proactive_read"], side_effect="read"),
            ToolSchema("security_set_scoped_permission", "범위별 권한 설정", {"type":"object","properties":{
                "permission_id":{"type":"string"},"scope_type":{"enum":["folder","app","domain","account","global"]},
                "scope_value":{"type":"string"},"lifetime":{"enum":["once","session","always"]},
                "decision":{"enum":["allow","block"]}},"required":["permission_id","scope_type","scope_value","lifetime","decision"],"additionalProperties":False}, ["proactive_manage"], side_effect="change"),
            ToolSchema("security_credential_store", "Credential Manager에 비밀 저장", {"type":"object","properties":{"name":{"type":"string"},"secret":{"type":"string"}},"required":["name","secret"],"additionalProperties":False}, ["cloud_account"], side_effect="change"),
            ToolSchema("security_credential_delete", "Credential Manager 비밀 삭제", {"type":"object","properties":{"name":{"type":"string"}},"required":["name"],"additionalProperties":False}, ["cloud_account"], side_effect="change"),
            ToolSchema("runtime_metrics", "런타임 메트릭 조회", obj, ["proactive_read"], side_effect="read"),
            ToolSchema("runtime_diagnostic_report", "마스킹된 진단 보고서 생성", {"type":"object","properties":{"output":{"type":"string"}},"additionalProperties":False}, ["proactive_read"], side_effect="read"),
            ToolSchema("runtime_safe_mode", "안전 모드 설정", {"type":"object","properties":{"enabled":{"type":"boolean"},"reason":{"type":"string"}},"required":["enabled"],"additionalProperties":False}, ["proactive_manage"], side_effect="change"),
        ]

    def execute_tool(self, name, data):
        try:
            detail = {}
            if name == "security_list_scoped_permissions": detail = {"grants": get_permission_manager().list_scoped_permissions()}
            elif name == "security_set_scoped_permission": detail = {"grant": get_permission_manager().grant_scoped(**data).__dict__}
            elif name == "security_credential_store": WindowsCredentialVault().set(data["name"], data["secret"]); detail = {"stored": True, "name": data["name"]}
            elif name == "security_credential_delete": detail = {"deleted": WindowsCredentialVault().delete(data["name"]), "name": data["name"]}
            elif name == "runtime_metrics": detail = METRICS.snapshot()
            elif name == "runtime_safe_mode": SafeModeManager().set(data["enabled"], data.get("reason", "")); detail = {"enabled": data["enabled"]}
            elif name == "runtime_diagnostic_report":
                output = data.get("output", "data/diagnostics/latest.json")
                detail = DiagnosticReporter().generate(output, databases=["data/action_journal.db"]); detail["output"] = str(Path(output).resolve())
            else: raise ValueError("Unsupported productization tool")
            return ToolRunResult.successful(tool_name=name, raw_output="요청을 처리했습니다.", evidence=[Evidence("productization", "P12 runtime result", detail)])
        except Exception as exc: return ToolRunResult.failed(tool_name=name, error=str(exc))
