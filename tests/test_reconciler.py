import json
import shutil
from pathlib import Path

import yaml

from normalizer.models import IssueSeverity
from normalizer.reconciler import reconcile_data_directory


def test_reconcile_example_repo_copy(tmp_path: Path) -> None:
    """Kopiuje brudne pliki z example_repo/grafana_dirty do tmp_path i sprawdza pełną normalizację + idempotentność."""
    test_data = tmp_path / "data"
    shutil.copytree("example_repo/grafana_dirty", test_data)

    # 1. Pierwsze uruchomienie: normalizacja surowych plików
    res1 = reconcile_data_directory(test_data)
    assert not res1.has_errors
    assert len(res1.issues) == 0

    # Surowe pliki .json i modify-export-*.yaml powinny zostać usunięte
    assert not (test_data / "PRD - Applications" / "Series active-1793109192093.json").exists()
    assert not (
        test_data / "PRD - Infrastructure" / "Usage" / "Active users-1793109129935.json"
    ).exists()
    assert not (
        test_data
        / "PRD - Infrastructure"
        / "Usage"
        / "modify-export-Usage alerts-cg2hxgz5619fkd-1793109505220.yaml"
    ).exists()

    # Pliki readme.md powinny pozostać nietknięte
    assert (test_data / "readme.md").exists()
    assert (test_data / "PRD - Infrastructure" / "Usage" / "readme.md").exists()

    # Kanoniczne pliki zasobów (d-* / a-*) oraz folder.yaml muszą istnieć
    assert (test_data / "PRD - Applications" / "folder.yaml").exists()
    assert (test_data / "PRD - Applications" / "d-series-active.yaml").exists()
    assert (test_data / "PRD - Infrastructure" / "folder.yaml").exists()
    assert (test_data / "PRD - Infrastructure" / "Usage" / "folder.yaml").exists()
    assert (test_data / "PRD - Infrastructure" / "Usage" / "d-active-users.yaml").exists()
    assert (test_data / "PRD - Infrastructure" / "Usage" / "a-usage-alert.yaml").exists()

    # Sprawdźmy relację rodzic-dziecko pomiędzy folderami PRD - Infrastructure -> Usage
    parent_folder = yaml.safe_load(
        (test_data / "PRD - Infrastructure" / "folder.yaml").read_text(encoding="utf-8")
    )
    child_folder = yaml.safe_load(
        (test_data / "PRD - Infrastructure" / "Usage" / "folder.yaml").read_text(encoding="utf-8")
    )
    assert "annotations" not in parent_folder["metadata"]
    assert (
        child_folder["metadata"]["annotations"]["grafana.app/folder"]
        == parent_folder["metadata"]["name"]
    )

    # 2. Drugie uruchomienie na tym samym katalogu: 100% IDEMPOTENTNOŚĆ (zero zapisanych/usuniętych plików)
    res2 = reconcile_data_directory(test_data)
    assert not res2.has_errors
    assert res2.written_files == []
    assert res2.deleted_raw_files == []
    assert res2.deleted_empty_folders == []


def test_depth_limit_exceeded_ignored_with_warning(tmp_path: Path) -> None:
    # Tworzymy 5 poziomów zagnieżdżenia: L1 / L2 / L3 / L4 / L5
    deep_dir = tmp_path / "L1" / "L2" / "L3" / "L4" / "L5"
    deep_dir.mkdir(parents=True)
    raw_dash = deep_dir / "too-deep.json"
    raw_dash.write_text(
        json.dumps({"title": "Too Deep Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )

    res = reconcile_data_directory(tmp_path)
    assert not res.has_errors
    assert len(res.issues) == 1
    assert res.issues[0].severity == IssueSeverity.WARNING
    assert "depth 5" in res.issues[0].message
    # Plik na 5 poziomie nie został ruszony i nie utworzono folder.yaml
    assert raw_dash.exists()
    assert not (deep_dir / "folder.yaml").exists()


def test_update_existing_canonical_dashboard_from_new_raw_export(tmp_path: Path) -> None:
    folder = tmp_path / "PRD - Applications"
    folder.mkdir()

    # Najpierw tworzymy pierwszy eksport
    raw1 = folder / "export-1.json"
    raw1.write_text(
        json.dumps({"title": "My App", "schemaVersion": 39, "panels": [{"id": 1}]}),
        encoding="utf-8",
    )
    reconcile_data_directory(tmp_path)
    assert (folder / "d-my-app.yaml").exists()

    # Teraz user wrzuca nowy surowy eksport z tym samym tytułem ("My App"), ale nowym panelem
    raw2 = folder / "My App-999999.json"
    raw2.write_text(
        json.dumps({"title": "My App", "schemaVersion": 39, "panels": [{"id": 1}, {"id": 2}]}),
        encoding="utf-8",
    )
    res_update = reconcile_data_directory(tmp_path)
    assert not res_update.has_errors
    assert not raw2.exists()
    updated = yaml.safe_load((folder / "d-my-app.yaml").read_text(encoding="utf-8"))
    assert len(updated["spec"]["panels"]) == 2


def test_collision_two_raw_exports_with_same_title_fails(tmp_path: Path) -> None:
    folder = tmp_path / "PRD - Applications"
    folder.mkdir()

    for idx in (1, 2):
        (folder / f"export-{idx}.json").write_text(
            json.dumps({"title": "Duplicate Title", "schemaVersion": 39, "panels": []}),
            encoding="utf-8",
        )

    res = reconcile_data_directory(tmp_path)
    assert res.has_errors
    assert any("Name collision" in issue.message for issue in res.issues)


def test_empty_folder_manifest_is_cleaned_up(tmp_path: Path) -> None:
    folder = tmp_path / "PRD - Applications" / "Subfolder"
    folder.mkdir(parents=True)
    raw = folder / "dash.json"
    raw.write_text(
        json.dumps({"title": "Temp Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )

    reconcile_data_directory(tmp_path)
    assert (folder / "folder.yaml").exists()
    assert (folder.parent / "folder.yaml").exists()

    # Teraz user usuwa dashboard
    (folder / "d-temp-dash.yaml").unlink()

    res_cleanup = reconcile_data_directory(tmp_path)
    assert not (folder / "folder.yaml").exists()
    assert not (folder.parent / "folder.yaml").exists()
    assert len(res_cleanup.deleted_empty_folders) == 2


def test_external_sharing_export_with_inputs_rejected(tmp_path: Path) -> None:
    """Sprawdza odrzucenie eksportu z zaznaczoną opcją 'Export for sharing externally' (__inputs)."""
    folder = tmp_path / "PRD - Applications"
    folder.mkdir()
    raw = folder / "external.json"
    raw.write_text(
        json.dumps(
            {
                "__inputs": [{"name": "DS_PROM", "type": "datasource"}],
                "title": "External Dash",
                "schemaVersion": 39,
                "panels": [],
            }
        ),
        encoding="utf-8",
    )

    res = reconcile_data_directory(tmp_path)
    assert res.has_errors
    assert any("Export for sharing externally" in issue.message for issue in res.issues)


def test_resource_directly_in_data_root_warns(tmp_path: Path) -> None:
    """Sprawdza ostrzeżenie, gdy użytkownik wrzuci dashboard bezpośrednio do głównego folderu data/."""
    raw = tmp_path / "loose-dash.json"
    raw.write_text(
        json.dumps({"title": "Loose Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )

    res = reconcile_data_directory(tmp_path)
    assert not res.has_errors
    assert any("outside any Grafana folder" in issue.message for issue in res.issues)


def test_raw_ui_recording_rule_group_with_dict_metric_names_correctly(tmp_path: Path) -> None:
    """
    Sprawdza, czy eksport z UI z apiVersion: 1 gdzie pole `record` lub `metric` jest słownikiem:
    - Za pierwszym razem tworzy czysty plik `r-recruletest.yaml` (a NIE `r-metric-recruletest-...yaml`).
    - Za drugim razem jest w 100% idempotentny (zero zmian).
    """
    metrics_dir = tmp_path / "PRD" / "Metrics"
    metrics_dir.mkdir(parents=True)

    raw_rule_group = {
        "apiVersion": 1,
        "groups": [
            {
                "name": "1minute",
                "interval": "1m",
                "rules": [
                    {
                        "uid": "eg346hevv6m0wf",
                        "title": "recruletest",
                        "data": [
                            {
                                "refId": "A",
                                "datasourceUid": "grafanacloud-play-prom",
                                "model": {"expr": "rate(http[5m])"},
                            }
                        ],
                        "record": {
                            "metric": "recruletest",
                            "from": "A",
                            "targetDatasourceUid": "grafanacloud-prom",
                        },
                    }
                ],
            }
        ],
    }
    (metrics_dir / "export.yaml").write_text(yaml.safe_dump(raw_rule_group), encoding="utf-8")

    # 1. Pierwsze uruchomienie
    res1 = reconcile_data_directory(tmp_path)
    assert not res1.has_errors
    assert not (metrics_dir / "export.yaml").exists()

    canonical_file = metrics_dir / "r-recruletest.yaml"
    assert canonical_file.exists(), "Plik powinien mieć czytelną nazwę r-recruletest.yaml już przy pierwszym przebiegu!"
    # Upewniamy się, że nie powstał potworek ze stringified dictem
    assert not (metrics_dir / "r-metric-recruletest-from-a-targetdatasourceuid-grafanacloud-prom.yaml").exists()

    content = yaml.safe_load(canonical_file.read_text(encoding="utf-8"))
    assert content["kind"] == "RecordingRule"
    assert content["spec"]["title"] == "recruletest"
    assert content["spec"]["metric"] == "recruletest"
    assert content["spec"]["targetDatasourceUID"] == "grafanacloud-prom"
    assert content["spec"]["expressions"]["A"]["source"] is True

    # 2. Drugie uruchomienie (idempotentność)
    res2 = reconcile_data_directory(tmp_path)
    assert not res2.has_errors
    assert res2.written_files == []
    assert canonical_file.exists()


def test_mixed_alert_and_recording_rules_group_export_split_into_individual_files(
    tmp_path: Path,
) -> None:
    """
    Sprawdza, czy eksport grupy zawierającej MIESZANE reguły (alert rules oraz recording rules)
    zostaje prawidłowo rozbity na osobne kanoniczne pliki (a-*.yaml oraz r-*.yaml).
    """
    app_dir = tmp_path / "PRD" / "Applications"
    app_dir.mkdir(parents=True)

    mixed_group = {
        "apiVersion": 1,
        "groups": [
            {
                "name": "services_1m",
                "interval": "1m",
                "rules": [
                    {
                        "uid": "alert-1",
                        "title": "High CPU Usage",
                        "data": [{"refId": "A", "model": {"expr": "cpu > 80"}}],
                        "for": "5m",
                    },
                    {
                        "uid": "rec-1",
                        "title": "request_rate_5m",
                        "data": [
                            {
                                "refId": "A",
                                "datasourceUid": "prom-ds",
                                "model": {"expr": "rate(http[5m])"},
                            }
                        ],
                        "record": {
                            "metric": "request_rate_5m",
                            "from": "A",
                            "targetDatasourceUid": "prom-ds",
                        },
                    },
                    {
                        "uid": "rec-2",
                        "title": "error_rate_5m",
                        "data": [
                            {
                                "refId": "A",
                                "datasourceUid": "prom-ds",
                                "model": {"expr": "rate(http_errors[5m])"},
                            }
                        ],
                        "record": {
                            "metric": "error_rate_5m",
                            "from": "A",
                            "targetDatasourceUid": "prom-ds",
                        },
                    },
                ],
            }
        ],
    }
    (app_dir / "mixed_export.yaml").write_text(yaml.safe_dump(mixed_group), encoding="utf-8")

    res = reconcile_data_directory(tmp_path)
    assert not res.has_errors
    # Surowy plik grupy został usunięty
    assert not (app_dir / "mixed_export.yaml").exists()

    # Sprawdzamy wygenerowane pliki kanoniczne
    alert_file = app_dir / "a-high-cpu-usage.yaml"
    rec1_file = app_dir / "r-request-rate-5m.yaml"
    rec2_file = app_dir / "r-error-rate-5m.yaml"

    assert alert_file.exists()
    assert rec1_file.exists()
    assert rec2_file.exists()

    # Weryfikacja rodzajów i prefiksów UID
    alert_data = yaml.safe_load(alert_file.read_text(encoding="utf-8"))
    assert alert_data["kind"] == "AlertRule"
    assert alert_data["metadata"]["name"].startswith("a-")

    rec1_data = yaml.safe_load(rec1_file.read_text(encoding="utf-8"))
    assert rec1_data["kind"] == "RecordingRule"
    assert rec1_data["metadata"]["name"].startswith("r-")
    assert rec1_data["spec"]["metric"] == "request_rate_5m"

    rec2_data = yaml.safe_load(rec2_file.read_text(encoding="utf-8"))
    assert rec2_data["kind"] == "RecordingRule"
    assert rec2_data["metadata"]["name"].startswith("r-")
    assert rec2_data["spec"]["metric"] == "error_rate_5m"


def test_reconcile_migrates_alert_and_recording_rules_to_new_api_version(tmp_path: Path) -> None:
    """Sprawdza, czy po zmianie CANONICAL_ALERT_API_VERSION istniejące kanoniczne pliki v0alpha1 są aktualizowane do v1beta1."""
    from unittest.mock import patch

    from normalizer.paths import generate_uid

    folder = tmp_path / "PRD - Applications"
    folder.mkdir()

    expected_alert_uid = generate_uid("a", "PRD - Applications", "CPU Alert")
    expected_rec_uid = generate_uid("r", "PRD - Applications", "cpu_metric")

    alert_path = folder / "a-cpu-alert.yaml"
    alert_manifest = {
        "apiVersion": "rules.alerting.grafana.app/v0alpha1",
        "kind": "AlertRule",
        "metadata": {
            "name": expected_alert_uid,
            "annotations": {"grafana.app/folder": "f-22222222222222222222"},
        },
        "spec": {
            "title": "CPU Alert",
            "trigger": {"interval": "1m"},
            "expressions": {"A": {"source": True}},
        },
    }
    alert_path.write_text(yaml.safe_dump(alert_manifest), encoding="utf-8")

    rec_path = folder / "r-cpu-metric.yaml"
    rec_manifest = {
        "apiVersion": "rules.alerting.grafana.app/v0alpha1",
        "kind": "RecordingRule",
        "metadata": {
            "name": expected_rec_uid,
            "annotations": {"grafana.app/folder": "f-22222222222222222222"},
        },
        "spec": {
            "title": "cpu_metric",
            "metric": "cpu_metric",
            "targetDatasourceUID": "prom-ds",
            "expressions": {"A": {"source": True}},
            "trigger": {"interval": "1m"},
        },
    }
    rec_path.write_text(yaml.safe_dump(rec_manifest), encoding="utf-8")

    # Symulujemy zmianę wersji kanonicznej na v1beta1
    with patch("normalizer.transformer.CANONICAL_ALERT_API_VERSION", "rules.alerting.grafana.app/v1beta1"):
        res = reconcile_data_directory(tmp_path)

    assert not res.has_errors
    assert alert_path in res.written_files
    assert rec_path in res.written_files

    # Weryfikacja zaktualizowanych plików na dysku
    updated_alert = yaml.safe_load(alert_path.read_text(encoding="utf-8"))
    assert updated_alert["apiVersion"] == "rules.alerting.grafana.app/v1beta1"
    assert updated_alert["metadata"]["name"] == expected_alert_uid

    updated_rec = yaml.safe_load(rec_path.read_text(encoding="utf-8"))
    assert updated_rec["apiVersion"] == "rules.alerting.grafana.app/v1beta1"
    assert updated_rec["metadata"]["name"] == expected_rec_uid

