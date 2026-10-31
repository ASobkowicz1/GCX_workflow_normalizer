from pathlib import Path

from normalizer.models import FileKind, ParsedFile
from normalizer.parser import detect_and_parse
from normalizer.transformer import (
    build_folder_manifest,
    normalize_alertrule,
    normalize_dashboard,
)


def test_build_folder_manifest_root_and_nested() -> None:
    root_folder = build_folder_manifest("PRD - Infrastructure", "f-11111111111111111111")
    assert root_folder["apiVersion"] == "folder.grafana.app/v1beta1"
    assert root_folder["kind"] == "Folder"
    assert root_folder["metadata"] == {"name": "f-11111111111111111111"}
    assert root_folder["spec"]["title"] == "PRD - Infrastructure"

    nested_folder = build_folder_manifest(
        "Usage",
        "f-22222222222222222222",
        parent_uid="f-11111111111111111111",
    )
    assert nested_folder["metadata"]["name"] == "f-22222222222222222222"
    assert nested_folder["metadata"]["annotations"] == {
        "grafana.app/folder": "f-11111111111111111111"
    }


def test_normalize_dashboard_v2_from_example_repo() -> None:
    path = Path("example_repo/grafana_dirty/PRD - Applications/Series active-1793109192093.json")
    parsed = detect_and_parse(path)

    result = normalize_dashboard(
        parsed,
        folder_uid="f-aaaaaaaaaaaaaaaaaaaa",
        dashboard_uid="d-bbbbbbbbbbbbbbbbbbbb",
    )

    assert result["apiVersion"] == "dashboard.grafana.app/v2"
    assert result["kind"] == "Dashboard"
    # Sprawdzamy czy śmieci serwerowe (uid, resourceVersion, namespace) zniknęły z metadata
    assert result["metadata"] == {
        "name": "d-bbbbbbbbbbbbbbbbbbbb",
        "annotations": {"grafana.app/folder": "f-aaaaaaaaaaaaaaaaaaaa"},
    }
    assert result["spec"]["title"] == "Series active"


def test_normalize_dashboard_classic_v1(tmp_path: Path) -> None:
    parsed = ParsedFile(
        path=tmp_path / "raw.json",
        kind=FileKind.RAW_UI_DASHBOARD,
        title="Classic Dash",
        data={
            "id": 42,
            "uid": "old-uid",
            "version": 7,
            "folderUid": "old-folder",
            "__inputs": [{"name": "DS_PROM"}],
            "title": "Classic Dash",
            "schemaVersion": 39,
            "panels": [],
        },
    )

    result = normalize_dashboard(
        parsed,
        folder_uid="f-12345678901234567890",
        dashboard_uid="d-09876543210987654321",
    )

    assert result["apiVersion"] == "dashboard.grafana.app/v1beta1"
    assert result["metadata"]["name"] == "d-09876543210987654321"
    for stripped in ("id", "uid", "version", "folderUid", "__inputs"):
        assert stripped not in result["spec"]
    assert result["spec"]["title"] == "Classic Dash"


def test_normalize_alertrule_from_example_repo() -> None:
    path = Path(
        "example_repo/grafana_dirty/PRD - Infrastructure/Usage/"
        "modify-export-Usage alerts-cg2hxgz5619fkd-1793109505220.yaml"
    )
    parsed = detect_and_parse(path)

    result = normalize_alertrule(
        parsed,
        folder_uid="f-cccccccccccccccccccc",
        alert_uid="a-dddddddddddddddddddd",
    )

    assert result["apiVersion"] == "rules.alerting.grafana.app/v0alpha1"
    assert result["kind"] == "AlertRule"
    assert result["metadata"] == {
        "name": "a-dddddddddddddddddddd",
        "annotations": {"grafana.app/folder": "f-cccccccccccccccccccc"},
    }
    spec = result["spec"]
    assert spec["title"] == "Usage alert"
    assert spec["trigger"] == {"interval": "1m"}
    assert spec["expressions"]["A"]["datasourceUID"] == "grafanacloud-usage"
    assert spec["expressions"]["A"]["relativeTimeRange"] == {"from": "600s", "to": "0s"}
    assert spec["expressions"]["C"]["source"] is True
    assert spec["notificationSettings"] == {"receiver": "empty"}


def test_normalize_alertrule_preserves_v1beta1() -> None:
    parsed = ParsedFile(
        path=Path("alert.yaml"),
        kind=FileKind.RAW_UI_ALERTRULE,
        data={
            "apiVersion": "rules.alerting.grafana.app/v1beta1",
            "kind": "AlertRule",
            "metadata": {"name": "raw-name", "uid": "123"},
            "spec": {"title": "Beta Alert", "for": "5m"},
        },
    )
    result = normalize_alertrule(
        parsed,
        folder_uid="f-11111111111111111111",
        alert_uid="a-22222222222222222222",
    )
    assert result["apiVersion"] == "rules.alerting.grafana.app/v1beta1"
    assert result["metadata"]["name"] == "a-22222222222222222222"
    assert result["metadata"]["annotations"] == {"grafana.app/folder": "f-11111111111111111111"}
    assert result["spec"]["title"] == "Beta Alert"
