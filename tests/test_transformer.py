from pathlib import Path

from normalizer.models import FileKind, ParsedFile
from normalizer.parser import detect_and_parse
from normalizer.transformer import (
    CANONICAL_ALERT_API_VERSION,
    build_folder_manifest,
    normalize_alertrule,
    normalize_dashboard,
    normalize_recordingrule,
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
    assert spec["notificationSettings"] == {
        "receiver": "empty",
        "type": "SimplifiedRouting",
    }


def test_normalize_alertrule_enforces_canonical_api_version() -> None:
    parsed = ParsedFile(
        path=Path("alert.yaml"),
        kind=FileKind.RAW_UI_ALERTRULE,
        data={
            "apiVersion": "rules.alerting.grafana.app/legacy-version",
            "kind": "AlertRule",
            "metadata": {"name": "raw-name", "uid": "123"},
            "spec": {"title": "Test Alert", "for": "5m"},
        },
    )
    result = normalize_alertrule(
        parsed,
        folder_uid="f-11111111111111111111",
        alert_uid="a-22222222222222222222",
    )
    assert result["apiVersion"] == CANONICAL_ALERT_API_VERSION
    assert result["metadata"]["name"] == "a-22222222222222222222"
    assert result["metadata"]["annotations"] == {"grafana.app/folder": "f-11111111111111111111"}
    assert result["spec"]["title"] == "Test Alert"


def test_normalize_alertrule_notification_settings_simplified_routing() -> None:
    parsed = ParsedFile(
        path=Path("alert.yaml"),
        kind=FileKind.RAW_UI_ALERTRULE,
        data={
            "apiVersion": 1,
            "groups": [
                {
                    "name": "g1",
                    "interval": "1m",
                    "rules": [
                        {
                            "title": "Slack Alert",
                            "notification_settings": {"receiver": "slack-channel"},
                        }
                    ],
                }
            ],
        },
    )
    result = normalize_alertrule(parsed, "f-11111111111111111111", "a-22222222222222222222")
    assert result["spec"]["notificationSettings"] == {
        "receiver": "slack-channel",
        "type": "SimplifiedRouting",
    }


def test_normalize_recordingrule_k8s_and_raw(tmp_path: Path) -> None:
    """Sprawdza normalizację reguły RecordingRule z formatu k8s (GCX) z oczyszczeniem metadanych."""
    raw_k8s = {
        "apiVersion": "rules.alerting.grafana.app/v0alpha1",
        "kind": "RecordingRule",
        "metadata": {
            "annotations": {
                "grafana.app/folder": "ftjc75",
                "grafana.app/updatedBy": "efeq716xkuy2oe",
                "grafana.app/updatedTimestamp": "2026-11-02T19:37:12Z",
            },
            "name": "eg346hevv6m0wf",
            "namespace": "stacks-1542617",
            "resourceVersion": "1",
            "uid": "4954dac3-74d5-4187-8866-286ee667140a",
        },
        "spec": {
            "title": "recruletest",
            "metric": "recruletest",
            "targetDatasourceUID": "grafanacloud-prom",
            "trigger": {"interval": "1m"},
            "expressions": {
                "A": {
                    "datasourceUID": "grafanacloud-play-prom",
                    "model": {"expr": "avg(rate(http_requests[5m]))"},
                }
            },
        },
    }
    file_path = tmp_path / "raw-recrule.yaml"
    import yaml

    file_path.write_text(yaml.safe_dump(raw_k8s), encoding="utf-8")
    parsed = detect_and_parse(file_path)
    assert parsed.kind == FileKind.RAW_UI_RECORDINGRULE

    res = normalize_recordingrule(
        parsed,
        folder_uid="f-11111111111111111111",
        recordingrule_uid="r-22222222222222222222",
    )
    assert res["apiVersion"] == CANONICAL_ALERT_API_VERSION
    assert res["kind"] == "RecordingRule"
    assert res["metadata"] == {
        "name": "r-22222222222222222222",
        "annotations": {"grafana.app/folder": "f-11111111111111111111"},
    }
    assert res["spec"]["title"] == "recruletest"
    assert res["spec"]["metric"] == "recruletest"
    assert res["spec"]["targetDatasourceUID"] == "grafanacloud-prom"
    assert res["spec"]["expressions"]["A"]["source"] is True
    assert "uid" not in res["metadata"]
    assert "resourceVersion" not in res["metadata"]


def test_normalize_recordingrule_nested_metric_dict(tmp_path: Path) -> None:
    """Sprawdza normalizację reguły RecordingRule ze słownikiem `metric` w `spec` (format eksportu z UI)."""
    raw_ui = {
        "apiVersion": "rules.alerting.grafana.app/v0alpha1",
        "kind": "RecordingRule",
        "metadata": {
            "name": "raw-recording-rule",
            "uid": "raw-recording-rule",
            "resourceVersion": "2",
        },
        "spec": {
            "title": "recruletest",
            "metric": {
                "metric": "recruletest",
                "from": "A",
                "targetDatasourceUid": "grafanacloud-prom",
            },
            "expressions": {
                "A": {
                    "datasourceUID": "grafanacloud-play-prom",
                    "model": {"expr": "rate(http_requests[5m])"},
                }
            },
            "trigger": {"interval": "1m"},
        },
    }
    file_path = tmp_path / "raw-recrule-dict.yaml"
    import yaml

    file_path.write_text(yaml.safe_dump(raw_ui), encoding="utf-8")
    parsed = detect_and_parse(file_path)
    assert parsed.kind == FileKind.RAW_UI_RECORDINGRULE

    res = normalize_recordingrule(
        parsed,
        folder_uid="f-11111111111111111111",
        recordingrule_uid="r-22222222222222222222",
    )
    assert res["apiVersion"] == CANONICAL_ALERT_API_VERSION
    assert res["kind"] == "RecordingRule"
    assert res["metadata"]["name"] == "r-22222222222222222222"
    assert res["metadata"]["annotations"] == {"grafana.app/folder": "f-11111111111111111111"}
    assert res["spec"]["title"] == "recruletest"
    assert res["spec"]["metric"] == "recruletest"
    assert isinstance(res["spec"]["metric"], str)
    assert res["spec"]["targetDatasourceUID"] == "grafanacloud-prom"
    assert res["spec"]["expressions"]["A"]["source"] is True
