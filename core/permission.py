"""
Core Permission Manager & Capability Registry
- 권한 레벨 관리 (SAFE/CONFIRM/SYSTEM)
- Capability Registry (가용 플러그인·권한 상태 관리)
- 권한 요청 UI 연동 준비
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Callable
from datetime import datetime
import json


class PermissionLevel(Enum):
    SAFE = "safe"  # 자동 실행 OK (읽기, 검색, 계산 등)
    CONFIRM = "confirm"  # 매번 승인 필요 (삭제, 메일 전송, Git Push 등)
    SYSTEM = "system"  # 1회 승인 후 유지 (카메라, 마이크, 클립보드 등)
    DENIED = "denied"  # 영구 거부


class PermissionDecision(Enum):
    UNDECIDED = "undecided"
    ALLOW = "allow"
    BLOCK = "block"


@dataclass
class Permission:
    id: str
    name: str
    description: str
    level: PermissionLevel
    granted: bool = False
    decision: PermissionDecision = PermissionDecision.UNDECIDED
    granted_at: Optional[datetime] = None
    last_used: Optional[datetime] = None


@dataclass
class Capability:
    id: str
    name: str
    description: str
    plugin_id: str
    enabled: bool = True
    required_permissions: List[str] = field(default_factory=list)


# 도구 이름 → 필요한 Permission id 매핑 (단일 진실 공급원).
# 여기 없는 도구는 위험도가 낮다고 분류된 것으로 보고 별도 확인 없이 실행을 허용합니다
# (읽기/조회 전용 도구, 자기 자신의 프로필·의미기억·지식그래프에 대한 일반적인 조회/추가 등).
# 새 도구(특히 파일시스템 쓰기/삭제, 외부 API 호출, 자동 실행류)를 추가할 때는 반드시
# 여기에도 등록해야 Permission 체크가 누락되지 않습니다.
TOOL_PERMISSION_MAP: Dict[str, str] = {
    # 파일 읽기
    "read_file": "filesystem_read",
    "list_directory": "filesystem_read",
    "get_file_info": "filesystem_read",
    "extract_text_from_pdf": "filesystem_read",
    # 파일 쓰기
    "write_file": "filesystem_write",
    "create_excel_file": "filesystem_write",
    "write_excel_cell": "filesystem_write",
    "create_directory": "filesystem_write",
    # 파일 삭제
    "delete_directory": "filesystem_delete",
    "filesystem_tree": "filesystem_read",
    "filesystem_search": "filesystem_read",
    "excel_create_workbook": "filesystem_write",
    "excel_read_workbook": "filesystem_read",
    "excel_write_cells": "filesystem_write",
    "word_create_document": "filesystem_write",
    "word_read_document": "filesystem_read",
    "powerpoint_create_presentation": "filesystem_write",
    "powerpoint_read_presentation": "filesystem_read",
    "pdf_create_document": "filesystem_write",
    "pdf_extract_text": "filesystem_read",
    "hwpx_create_document": "filesystem_write",
    "hwpx_read_document": "filesystem_read",
    "windows_find_apps": "windows_api",
    "windows_launch_app": "windows_api",
    "windows_focus_window": "windows_api",
    "windows_add_app_aliases": "windows_api",
    "windows_list_app_aliases": "windows_api",
    "windows_remove_app_aliases": "windows_api",
    # 셸 명령 실행 (가장 위험도 높은 도구)
    "run_command": "shell_execute",
    "git_status": "filesystem_read",
    "git_diff": "filesystem_read",
    "git_log": "filesystem_read",
    "git_commit": "git_commit",
    "git_push": "git_push",
    "git_pull": "git_push",
    "browser_get_text": "browser",
    "browser_screenshot": "browser",
    # 카메라/마이크
    "capture_camera": "camera",
    "list_camera_devices": "camera",
    "listen": "microphone",
    "list_audio_input_devices": "microphone",
    "start_wakeword_detection": "microphone",
    "start_clap_detection": "microphone",
    # 데이터(파일 아닌 것) 삭제
    "delete_schedule_job": "data_delete",
    "delete_semantic_memory": "data_delete",
    "delete_automation_job": "data_delete",
    "delete_entity": "data_delete",
    # 자동화/자율 실행 (나중에 스스로 실행되는 작업을 등록·구동)
    "add_schedule_job": "automation",
    "start_scheduler": "automation",
    "add_automation_job": "automation",
    "set_alarm": "automation",
    "toggle_automation_job": "automation",
    "start_automation_engine": "automation",
    "execute_multi_agent": "automation",
    "oauth_begin": "cloud_account",
    "oauth_complete": "cloud_account",
    "oauth_status": "cloud_read",
    "remote_create_draft": "cloud_read",
    "remote_apply_draft": "external_send",
    "cloud_sync_catalog": "cloud_read",
    "communication_read_summary": "cloud_read",
}


class PermissionManager:
    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = Path(storage_path) if storage_path else Path("data/permissions.json")
        self.storage_path.parent.mkdir(exist_ok=True)
        self.permissions: Dict[str, Permission] = {}
        self._default_permissions: List[Permission] = [
            Permission(id="filesystem_read", name="파일 읽기", description="파일·폴더 내용 읽기", level=PermissionLevel.SAFE),
            Permission(id="filesystem_write", name="파일 쓰기", description="파일 생성·수정", level=PermissionLevel.CONFIRM),
            Permission(id="filesystem_delete", name="파일 삭제", description="파일·폴더 삭제", level=PermissionLevel.CONFIRM),
            Permission(id="shell_execute", name="명령 실행", description="Shell 명령 실행", level=PermissionLevel.CONFIRM),
            Permission(id="camera", name="카메라", description="카메라 접근", level=PermissionLevel.SYSTEM),
            Permission(id="microphone", name="마이크", description="마이크 접근", level=PermissionLevel.SYSTEM),
            Permission(id="clipboard", name="클립보드", description="클립보드 접근", level=PermissionLevel.SYSTEM),
            Permission(id="git_commit", name="Git Commit", description="Git Commit 실행", level=PermissionLevel.CONFIRM),
            Permission(id="git_push", name="Git Push", description="Git Push 실행", level=PermissionLevel.CONFIRM),
            Permission(id="mail_send", name="메일 전송", description="메일 전송", level=PermissionLevel.CONFIRM),
            Permission(id="browser", name="브라우저", description="브라우저 자동 조작", level=PermissionLevel.SYSTEM),
            Permission(id="windows_api", name="Windows API", description="Windows 시스템 API 접근",
                       level=PermissionLevel.SYSTEM),
            Permission(id="data_delete", name="데이터 삭제", description="메모리·지식그래프·스케줄·자동화 작업 등 파일이 아닌 데이터 삭제",
                       level=PermissionLevel.CONFIRM),
            Permission(id="automation", name="자동화 실행", description="스케줄러·자동화 엔진·멀티에이전트 등 나중에 스스로 실행되는 작업 등록/구동",
                       level=PermissionLevel.CONFIRM),
            Permission(id="cloud_account", name="클라우드 계정 연결",
                       description="Google 또는 Microsoft OAuth 계정 연결 및 토큰 갱신",
                       level=PermissionLevel.SYSTEM),
            Permission(id="cloud_read", name="클라우드 조회",
                       description="메일·일정·파일·메신저 메타데이터 조회 및 로컬 동기화",
                       level=PermissionLevel.SYSTEM),
            Permission(id="external_send", name="외부 전송",
                       description="메일·일정·Slack·Teams 내용을 외부 서비스에 실제 반영",
                       level=PermissionLevel.CONFIRM),
        ]
        self._load()
        self._request_callback: Optional[Callable[[Permission], bool]] = None

    def _load(self):
        """기존 권한 상태 로드"""
        # 기본 권한으로 초기화
        for perm in self._default_permissions:
            self.permissions[perm.id] = perm
        # 저장된 상태가 있으면 불러오기
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for perm_id, perm_data in data.items():
                        if perm_id in self.permissions:
                            self.permissions[perm_id].granted = perm_data.get("granted", False)
                            saved_decision = perm_data.get("decision")
                            if saved_decision:
                                self.permissions[perm_id].decision = PermissionDecision(saved_decision)
                            elif perm_data.get("granted", False):
                                # 이전 형식에서 저장된 허용 상태를 그대로 영구 허용으로 승격한다.
                                self.permissions[perm_id].decision = PermissionDecision.ALLOW
                            self.permissions[perm_id].granted_at = datetime.fromisoformat(
                                perm_data["granted_at"]) if perm_data.get("granted_at") else None
                            self.permissions[perm_id].last_used = datetime.fromisoformat(
                                perm_data["last_used"]) if perm_data.get("last_used") else None
            except Exception as e:
                print(f"[PermissionManager] Load error: {e}")

    def _save(self):
        """현재 권한 상태 저장"""
        data = {}
        for perm_id, perm in self.permissions.items():
            data[perm_id] = {
                "granted": perm.granted,
                "decision": perm.decision.value,
                "granted_at": perm.granted_at.isoformat() if perm.granted_at else None,
                "last_used": perm.last_used.isoformat() if perm.last_used else None
            }
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def set_request_callback(self, callback: Callable[[Permission], bool]):
        """권한 요청 UI 콜백 설정 (사용자에게 물어보는 함수)"""
        self._request_callback = callback

    def request_permission(self, permission_id: str) -> bool:
        """권한 요청 (필요시 사용자에게 물어봄)"""
        if permission_id not in self.permissions:
            return False
        perm = self.permissions[permission_id]
        if perm.level == PermissionLevel.SAFE:
            perm.last_used = datetime.now()
            self._save()
            return True
        if perm.decision != PermissionDecision.UNDECIDED:
            perm.last_used = datetime.now()
            self._save()
            return perm.decision == PermissionDecision.ALLOW
        # 권한 요청 필요
        if self._request_callback:
            granted = self._request_callback(perm)
            if granted:
                perm.granted = True
                perm.granted_at = datetime.now()
                perm.decision = PermissionDecision.ALLOW
            else:
                perm.granted = False
                perm.granted_at = None
                perm.decision = PermissionDecision.BLOCK
            perm.last_used = datetime.now()
            self._save()
            return granted
        # 콜백이 없으면 CONFIRM은 거부, SYSTEM은 거부
        return False

    def grant_permission(self, permission_id: str):
        """권한 직접 부여"""
        if permission_id in self.permissions:
            self.permissions[permission_id].granted = True
            self.permissions[permission_id].decision = PermissionDecision.ALLOW
            self.permissions[permission_id].granted_at = datetime.now()
            self._save()

    def revoke_permission(self, permission_id: str):
        """권한 취소"""
        if permission_id in self.permissions:
            self.permissions[permission_id].granted = False
            self.permissions[permission_id].decision = PermissionDecision.BLOCK
            self.permissions[permission_id].granted_at = None
            self._save()

    def check_permission(self, permission_id: str) -> bool:
        """현재 권한 상태만 확인 (요청 안 함)"""
        if permission_id not in self.permissions:
            return False
        perm = self.permissions[permission_id]
        if perm.level == PermissionLevel.SAFE:
            return True
        return perm.decision == PermissionDecision.ALLOW

    def get_all_permissions(self) -> List[Permission]:
        """모든 권한 목록 반환"""
        return list(self.permissions.values())

    def get_permission_by_level(self, level: PermissionLevel) -> List[Permission]:
        """특정 레벨의 권한 목록 반환"""
        return [p for p in self.permissions.values() if p.level == level]


class CapabilityRegistry:
    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = Path(storage_path) if storage_path else Path("data/capabilities.json")
        self.storage_path.parent.mkdir(exist_ok=True)
        self.capabilities: Dict[str, Capability] = {}
        self._load()

    def _load(self):
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for cap_id, cap_data in data.items():
                        self.capabilities[cap_id] = Capability(
                            id=cap_id,
                            name=cap_data["name"],
                            description=cap_data["description"],
                            plugin_id=cap_data["plugin_id"],
                            enabled=cap_data.get("enabled", True),
                            required_permissions=cap_data.get("required_permissions", [])
                        )
            except Exception as e:
                print(f"[CapabilityRegistry] Load error: {e}")

    def _save(self):
        data = {}
        for cap_id, cap in self.capabilities.items():
            data[cap_id] = {
                "name": cap.name,
                "description": cap.description,
                "plugin_id": cap.plugin_id,
                "enabled": cap.enabled,
                "required_permissions": cap.required_permissions
            }
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def register_capability(self, capability: Capability):
        """새 Capability 등록"""
        self.capabilities[capability.id] = capability
        self._save()

    def unregister_capability(self, capability_id: str):
        """Capability 제거"""
        if capability_id in self.capabilities:
            del self.capabilities[capability_id]
            self._save()

    def enable_capability(self, capability_id: str):
        """Capability 활성화"""
        if capability_id in self.capabilities:
            self.capabilities[capability_id].enabled = True
            self._save()

    def disable_capability(self, capability_id: str):
        """Capability 비활성화"""
        if capability_id in self.capabilities:
            self.capabilities[capability_id].enabled = False
            self._save()

    def get_capability(self, capability_id: str) -> Optional[Capability]:
        return self.capabilities.get(capability_id)

    def get_all_capabilities(self) -> List[Capability]:
        return list(self.capabilities.values())

    def get_available_capabilities(self, permission_manager: PermissionManager) -> List[Capability]:
        """사용 가능한 Capability (권한이 충족된 것만)"""
        available = []
        for cap in self.capabilities.values():
            if not cap.enabled:
                continue
            has_all_perms = True
            for perm_id in cap.required_permissions:
                if not permission_manager.check_permission(perm_id):
                    has_all_perms = False
                    break
            if has_all_perms:
                available.append(cap)
        return available


# Singleton instances
_permission_manager = None
_capability_registry = None


def get_permission_manager() -> PermissionManager:
    global _permission_manager
    if _permission_manager is None:
        _permission_manager = PermissionManager()
    return _permission_manager


def get_capability_registry() -> CapabilityRegistry:
    global _capability_registry
    if _capability_registry is None:
        _capability_registry = CapabilityRegistry()
    return _capability_registry
