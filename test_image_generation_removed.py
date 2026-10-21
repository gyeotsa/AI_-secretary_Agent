from config import Config
from core.model_registry import ModelRegistry, ModelRoleRouter
from core.plugin import PluginRegistry
from core.specialist_team import SpecialistTeamRuntime
from core.specialist_workspaces import get_specialist_workspace_registry


def test_default_plugin_discovery_excludes_removed_image_generation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    monkeypatch.setattr(Config.API_CONFIG, "LEARNING_DB_PATH", str(tmp_path / "learning.db"))
    registry = PluginRegistry()
    try:
        registry.load_plugins_from_directory()
        names = {tool.name for tool in registry.get_all_tools()}
        assert {"filesystem_tree", "visual_analyze", "photoshop_edit_document"} <= names
        assert registry.get_plugin("mockup_design") is None
        assert not any(name.startswith("mockup_") for name in names)
        assert registry.get_capability("mockup_render") is None
        assert registry.get_capability("mockup_prepare_generation") is None
        assert not any("mockup" in name for name in registry._load_failures)
    finally:
        registry.shutdown()
        for name in list(registry.plugins):
            registry.unregister_plugin(name)


def test_removed_image_generation_has_no_specialist_contract_or_model_role():
    registry = get_specialist_workspace_registry()
    assert registry.get("mockup") is None
    assert "mockup" not in SpecialistTeamRuntime.TEAM_BLUEPRINTS
    assert {"document", "coding", "research", "photoshop", "knowledge_graph"} <= {
        item.key for item in registry.all()
    }
    profiles = ModelRegistry().profiles()
    assert not {"mockup_design", "style_vision", "visual_critic", "design_planning",
                "subject_analysis", "rendering"} & profiles.keys()
    assert profiles["vision"].modalities == profiles["computer_use"].modalities == ("text", "image")
    assert ModelRoleRouter().route(allowed_tools=["mockup_render"], modalities=["image"]) == "vision"
