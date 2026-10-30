import json
from pathlib import Path

import yaml

from normalizer.models import FileKind
from normalizer.parser import detect_and_parse


def test_ignored_non_yaml_json_files(tmp_path: Path) -> None:
    readme = tmp_path / "readme.md"
    readme.write_text("# Hello", encoding="utf-8")

    parsed = detect_and_parse(readme)
    assert parsed.kind == FileKind.IGNORED
    assert parsed.data is None


def test_invalid_corrupted_json_or_non_dict(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not valid json", encoding="utf-8")
    parsed_broken = detect_and_parse(broken)
    assert parsed_broken.kind == FileKind.INVALID
    assert parsed_broken.error is not None

    list_yaml = tmp_path / "list.yaml"
    list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
    parsed_list = detect_and_parse(list_yaml)
    assert parsed_list.kind == FileKind.INVALID


def test_detect_raw_ui_dashboard_v2_from_example_repo() -> None:
    path = Path("example_repo/data_dirty/PRD - Applications/Series active-1793109192093.json")
    parsed = detect_and_parse(path)

    assert parsed.kind == FileKind.RAW_UI_DASHBOARD
    assert parsed.title == "Series active"
    assert parsed.data is not None


def test_detect_raw_ui_dashboard_classic_v1(tmp_path: Path) -> None:
    classic_dash = tmp_path / "classic.json"
    classic_dash.write_text(
        json.dumps({"title": "Classic VM Dashboard", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )

    parsed = detect_and_parse(classic_dash)
    assert parsed.kind == FileKind.RAW_UI_DASHBOARD
    assert parsed.title == "Classic VM Dashboard"


def test_detect_canonical_dashboard(tmp_path: Path) -> None:
    canonical_dash = tmp_path / "series-active.yaml"
    manifest = {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {
            "name": "d-1234567890abcdef1234",
            "annotations": {"grafana.app/folder": "f-abcdef1234567890abcd"},
        },
        "spec": {"title": "Series active", "elements": {}},
    }
    canonical_dash.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    parsed = detect_and_parse(canonical_dash)
    assert parsed.kind == FileKind.CANONICAL_DASHBOARD
    assert parsed.title == "Series active"


def test_detect_canonical_folder(tmp_path: Path) -> None:
    folder_file = tmp_path / "folder.yaml"
    manifest = {
        "apiVersion": "folder.grafana.app/v1beta1",
        "kind": "Folder",
        "metadata": {"name": "f-abcdef1234567890abcd"},
        "spec": {"title": "PRD - Applications", "description": ""},
    }
    folder_file.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    parsed = detect_and_parse(folder_file)
    assert parsed.kind == FileKind.CANONICAL_FOLDER
    assert parsed.title == "PRD - Applications"


def test_detect_raw_ui_alertrule_from_example_repo() -> None:
    path = Path(
        "example_repo/data_dirty/PRD - Infrastructure/Usage/"
        "modify-export-Usage alerts-cg2hxgz5619fkd-1793109505220.yaml"
    )
    parsed = detect_and_parse(path)

    assert parsed.kind == FileKind.RAW_UI_ALERTRULE
    assert parsed.title == "Usage alert"
    assert parsed.data is not None


def test_detect_canonical_alertrule(tmp_path: Path) -> None:
    alert_file = tmp_path / "usage-alert.yaml"
    manifest = {
        "apiVersion": "rules.alerting.grafana.app/v0alpha1",
        "kind": "AlertRule",
        "metadata": {
            "name": "a-1234567890abcdef1234",
            "annotations": {"grafana.app/folder": "f-abcdef1234567890abcd"},
        },
        "spec": {"title": "Usage alert"},
    }
    alert_file.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    parsed = detect_and_parse(alert_file)
    assert parsed.kind == FileKind.CANONICAL_ALERTRULE
    assert parsed.title == "Usage alert"
