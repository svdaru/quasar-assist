"""Файлы упаковки, шкала качества и ограничения стиля."""

import ast
import json
import struct
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "quasar_assist"

DONE = {
    "brands",
    "config-flow",
    "config-flow-test-coverage",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "integration-owner",
    "log-when-unavailable",
    "test-coverage",
    "diagnostics",
    "docs-examples",
    "docs-known-limitations",
    "docs-supported-functions",
    "docs-troubleshooting",
    "docs-use-cases",
    "exception-translations",
    "reconfiguration-flow",
    "repair-issues",
    "strict-typing",
}

EXEMPT = {
    "action-setup",
    "action-exceptions",
    "appropriate-polling",
    "async-dependency",
    "common-modules",
    "dependency-transparency",
    "devices",
    "discovery",
    "discovery-update-info",
    "docs-actions",
    "docs-conditions",
    "docs-data-update",
    "docs-supported-devices",
    "docs-triggers",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-event-setup",
    "entity-translations",
    "entity-unavailable",
    "entity-unique-id",
    "has-entity-name",
    "icon-translations",
    "inject-websession",
    "parallel-updates",
    "reauthentication-flow",
    "stale-devices",
    "unique-config-entry",
}

README_SNIPPETS = (
    "Quasar Assist",
    "Marketplace",
    "long-lived",
    "Quasar Bridge 0.7.5",
    "Removal",
    "Installation parameters",
    "Configuration parameters",
    "Examples",
    "Supported functions",
    "Use case",
    "Known limitations",
    "Troubleshooting",
    "90",
    "llm_base_url",
    "responses",
)


def _python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_manifest_and_hacs() -> None:
    """Манифест и hacs.json совпадают с контрактом публикации."""
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest) == [
        "domain",
        "name",
        "categories",
        "codeowners",
        "config_flow",
        "dependencies",
        "documentation",
        "integration_type",
        "iot_class",
        "issue_tracker",
        "version",
    ]
    assert manifest["domain"] == "quasar_assist"
    assert manifest["name"] == "Quasar Assist"
    assert manifest["version"] == "0.2.0"
    assert manifest["config_flow"] is True
    assert manifest["dependencies"] == ["assist_pipeline"]
    assert manifest["documentation"] == (
        "https://github.com/svdaru/quasar-assist/blob/master/README.md"
    )
    assert manifest["iot_class"] == "local_push"
    assert manifest["codeowners"] == ["@svdaru"]
    assert manifest["documentation"].endswith("/README.md")
    assert manifest["issue_tracker"] == "https://github.com/svdaru/quasar-assist/issues"
    assert manifest["categories"] == ["ai_voice.voice_assistants"]
    assert "quality_scale" not in manifest
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "Quasar Assist"
    major, minor, *_rest = (int(part) for part in hacs["homeassistant"].split("."))
    assert (major, minor) >= (2026, 3)


def test_quality_scale_matches_prompt() -> None:
    """Правила из «Выполнить» отмечены done, изъятия — exempt с причиной."""
    text = (PACKAGE / "quality_scale.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    rules = document["rules"]
    assert set(rules) == DONE | EXEMPT
    for name in DONE:
        assert rules[name] == "done"
    for name in EXEMPT:
        assert rules[name]["status"] == "exempt"
        assert str(rules[name]["comment"]).strip() != ""


def test_readme_translations_and_icons() -> None:
    """README, переводы и значки закрывают упаковку Marketplace."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for snippet in README_SNIPPETS:
        assert snippet in readme
    assert "README.ru.md" in readme
    assert "pipeline" in readme.lower()
    russian = (ROOT / "README.ru.md").read_text(encoding="utf-8")
    assert "конвейер" in russian
    assert "90" in russian
    assert "/api/quasar_assist/v1/models" in russian
    assert "/api/quasar_assist/v1/responses" in russian
    assert "agent_id" not in readme
    assert "agent_id" not in russian
    assert not (PACKAGE / "strings.json").exists()
    for language in ("en", "ru"):
        translated = json.loads(
            (PACKAGE / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )
        errors = translated["config"]["error"]
        assert "pipeline_not_found" in errors
        assert "invalid_ttl" in errors
        assert "no_pipelines" in translated["config"]["abort"]
        assert "pipeline_unavailable" in translated["issues"]
        exception = translated["exceptions"]["pipeline_unavailable"]
        assert "{entry_id}" in exception["message"]
        assert "agent_id" not in json.dumps(translated)
    assert _png_size(PACKAGE / "brand" / "icon.png") == (256, 256)
    assert _png_size(PACKAGE / "brand" / "icon@2x.png") == (512, 512)


def test_workflows_and_release_tag() -> None:
    """Hassfest и HACS Action запускаются на push и pull request, без ignore."""
    for name in ("hassfest.yaml", "hacs.yaml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "push:" in text
        assert "pull_request:" in text
        assert "ignore" not in text
    hacs = (ROOT / ".github" / "workflows" / "hacs.yaml").read_text(encoding="utf-8")
    assert "category: integration" in hacs
    assert "hacs/action" in hacs
    hassfest_path = ROOT / ".github" / "workflows" / "hassfest.yaml"
    hassfest = hassfest_path.read_text(encoding="utf-8")
    assert "home-assistant/actions/hassfest" in hassfest
    tags = subprocess.run(
        ["git", "tag", "--list"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if tags:
        assert tags == ["0.1.0"]


def test_production_style_and_no_outbound_model() -> None:
    """Нет future-аннотаций, assert, широкого except Exception и исходящей модели."""
    forbidden_text = (
        "from __future__ import annotations",
        "async_get_clientsession",
        "api.openai.com",
        "time.sleep",
        "Authorization",
    )
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        for snippet in forbidden_text:
            assert snippet not in source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.Assert)
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None
                assert not _names_exception(node.type)
        if path.name in {"http.py", "__init__.py", "config_flow.py"}:
            functions = {
                item.name: item
                for item in tree.body
                if isinstance(
                    item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
            }
            assert functions
    http_tree = ast.parse((PACKAGE / "http.py").read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(http_tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert "post" in methods
    assert "get" in methods
    pipeline_tree = ast.parse((PACKAGE / "pipeline.py").read_text(encoding="utf-8"))
    pipeline_names = {
        node.name
        for node in ast.walk(pipeline_tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert "async_run_pipeline" in pipeline_names
    assert not (PACKAGE / "agent.py").exists()


def test_package_has_no_saved_agent_field() -> None:
    """В дереве компонента нет выбора агента диалога и сохранённого поля."""
    forbidden = (
        "agent_id",
        "async_converse",
        "async_get_agent",
        "async_list_agents",
        "HOME_ASSISTANT_AGENT",
        "voice assistant",
        "голосового ассистента",
        "голосовой ассистент",
    )
    for path in PACKAGE.rglob("*"):
        if path.suffix not in {".py", ".json", ".yaml", ".yml", ".md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} содержит {token}"


def _names_exception(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "Exception"
    if isinstance(node, ast.Tuple):
        return any(_names_exception(item) for item in node.elts)
    return False
