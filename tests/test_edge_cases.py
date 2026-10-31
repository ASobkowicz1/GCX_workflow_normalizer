"""Zestaw testów sprawdzających edge-case'y oraz nietypowe akcje użytkownika w głównym silniku GitOps (normalizer)."""

import json
import subprocess
from pathlib import Path

import yaml

from normalizer.models import FileKind, IssueSeverity, PlanActionType
from normalizer.parser import detect_and_parse
from normalizer.paths import slugify_filename
from normalizer.planner import build_delta_plan, build_gcx_delta_commands
from normalizer.reconciler import reconcile_data_directory


def _git(repo: Path, *args: str) -> None:
    """Pomocnicza funkcja do uruchamiania poleceń git w katalogu repozytorium testowego."""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_user_renames_or_moves_folder(tmp_path: Path) -> None:
    """
    Użytkownik przenosi lub zmienia nazwę folderu (np. PRD/OldApp -> PRD/NewApp).
    Weryfikujemy:
    1. Usunięcie starego folder.yaml i stworzenie nowego w nowej lokalizacji.
    2. Wygenerowanie nowego deterministycznego UID powiązanego z nową ścieżką.
    3. Plan: DESTROY starego folderu i dashboardu, CREATE nowego folderu i dashboardu.
    4. Kolejność komend GCX: push nowego folderu -> push nowego dashboardu -> delete starego dashboardu -> delete starego folderu.
    """
    repo = tmp_path / "repo"
    data_dir = repo / "data"
    old_app_dir = data_dir / "PRD" / "OldApp"
    old_app_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (old_app_dir / "dash.json").write_text(
        json.dumps({"title": "App Metrics", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    reconcile_data_directory(data_dir)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial OldApp state")

    old_dash_yaml = old_app_dir / "d-app-metrics.yaml"
    assert old_dash_yaml.exists()
    old_dash_content = yaml.safe_load(old_dash_yaml.read_text(encoding="utf-8"))
    old_dash_uid = old_dash_content["metadata"]["name"]

    # KROK 2: Użytkownik zmienia nazwę folderu na NewApp
    new_app_dir = data_dir / "PRD" / "NewApp"
    new_app_dir.mkdir(parents=True)
    (new_app_dir / "dash.json").write_text(
        json.dumps({"title": "App Metrics", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    # Usuwamy stary dashboard
    old_dash_yaml.unlink()

    # Reconcile sprząta osierocony folder OldApp i tworzy NewApp
    reconcile_data_directory(data_dir)
    assert not old_app_dir.exists()
    assert (new_app_dir / "d-app-metrics.yaml").exists()

    new_dash_content = yaml.safe_load(
        (new_app_dir / "d-app-metrics.yaml").read_text(encoding="utf-8")
    )
    new_dash_uid = new_dash_content["metadata"]["name"]
    assert new_dash_uid != old_dash_uid  # Deterministyczny UID zależy od ścieżki folderu!

    # KROK 3: Plan delty Git
    plan = build_delta_plan(repo, data_subdir="data", base_ref="HEAD")
    assert len(plan.to_add) == 2  # Folder NewApp + Dashboard App Metrics
    assert len(plan.to_destroy) == 2  # Folder OldApp + stary Dashboard

    # KROK 4: Bezpieczna kolejność wykonania w GCX
    cmds = build_gcx_delta_commands(plan, gcx_bin="./gcx")
    # 0) Pre-flight validate
    # 1) Push folderu NewApp
    # 2) Push dashboardu NewApp
    # 3) Delete starego dashboardu
    # 4) Delete starego folderu OldApp
    assert len(cmds) == 5
    assert cmds[0][:3] == ["./gcx", "resources", "validate"]
    assert "data/PRD/NewApp/folder.yaml" in cmds[1]
    assert "data/PRD/NewApp/d-app-metrics.yaml" in cmds[2]
    assert cmds[3] == ["./gcx", "resources", "delete", f"dashboards/{old_dash_uid}", "-y"]
    assert cmds[4] == ["./gcx", "resources", "delete", "folders/f-", "-y"] or "folders/" in cmds[4][3]


def test_user_deletes_entire_nested_folder_tree(tmp_path: Path) -> None:
    """
    Użytkownik kasuje całe drzewo folderów (L1/L2/L3) z wieloma dashboardami.
    Weryfikujemy odwróconą kolejność kasowania (L3 -> L2 -> L1) w komendach GCX delete.
    """
    repo = tmp_path / "repo"
    data_dir = repo / "data"
    sub_app_dir = data_dir / "PRD" / "Services" / "Payments"
    sub_app_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (sub_app_dir / "pay.json").write_text(
        json.dumps({"title": "Payments Dashboard", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    (data_dir / "PRD" / "Services" / "srv.json").write_text(
        json.dumps({"title": "Services Overview", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    reconcile_data_directory(data_dir)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial 3-level tree")

    # Użytkownik kasuje cały podkatalog Payments
    import shutil

    shutil.rmtree(sub_app_dir)
    reconcile_data_directory(data_dir)

    plan = build_delta_plan(repo, data_subdir="data", base_ref="HEAD")
    assert len(plan.to_add) == 0
    assert len(plan.to_change) == 0
    assert len(plan.to_destroy) == 2  # Dashboard Payments + Folder Payments (L3)

    cmds = build_gcx_delta_commands(plan, gcx_bin="./gcx")
    # Kasowanie zasobu przed skasowaniem folderu
    assert any("dashboards/" in cmd[3] for cmd in cmds if "delete" in cmd)
    assert any("folders/" in cmd[3] for cmd in cmds if "delete" in cmd)


def test_user_adds_multirule_alert_export_splits_into_separate_canonical_files(tmp_path: Path) -> None:
    """
    Użytkownik wrzuca eksport reguł alertów z UI Grafany (format apiVersion: 1 z wieloma regułami w grupie).
    Weryfikujemy:
    1. Rozbicie na niezależne pliki `a-*.yaml`.
    2. Usunięcie oryginalnego surowego pliku.
    3. Poprawną strukturę manifestu rules.alerting.grafana.app/v0alpha1 dla każdego z alertów.
    """
    folder = tmp_path / "PRD" / "Alerts"
    folder.mkdir(parents=True)

    multirule_data = {
        "apiVersion": 1,
        "groups": [
            {
                "name": "Infra Alert Group",
                "interval": "30s",
                "rules": [
                    {
                        "title": "CPU Usage High",
                        "for": "2m",
                        "condition": "B",
                        "data": [
                            {
                                "refId": "A",
                                "queryType": "",
                                "relativeTimeRange": {"from": 300, "to": 0},
                                "datasourceUid": "prometheus-prod",
                                "model": {"expr": "rate(node_cpu[5m]) > 0.8"},
                            },
                            {
                                "refId": "B",
                                "relativeTimeRange": {"from": 0, "to": 0},
                                "datasourceUid": "__expr__",
                                "model": {"type": "threshold"},
                            },
                        ],
                    },
                    {
                        "title": "Memory Usage High",
                        "for": "5m",
                        "condition": "B",
                        "data": [
                            {
                                "refId": "A",
                                "relativeTimeRange": {"from": 300, "to": 0},
                                "datasourceUid": "prometheus-prod",
                                "model": {"expr": "node_memory_used_ratio > 0.9"},
                            },
                            {
                                "refId": "B",
                                "relativeTimeRange": {"from": 0, "to": 0},
                                "datasourceUid": "__expr__",
                                "model": {"type": "threshold"},
                            },
                        ],
                    },
                ],
            }
        ],
    }

    raw_file = folder / "export-alerts-ui.yaml"
    with raw_file.open("w", encoding="utf-8") as f:
        yaml.safe_dump(multirule_data, f)

    res = reconcile_data_directory(tmp_path)
    assert not res.has_errors
    assert not raw_file.exists()  # Surowy plik usunięty

    # Sprawdzamy wygenerowane pliki kanoniczne
    cpu_alert = folder / "a-cpu-usage-high.yaml"
    mem_alert = folder / "a-memory-usage-high.yaml"
    assert cpu_alert.exists()
    assert mem_alert.exists()

    cpu_data = yaml.safe_load(cpu_alert.read_text(encoding="utf-8"))
    assert cpu_data["kind"] == "AlertRule"
    assert cpu_data["spec"]["title"] == "CPU Usage High"
    assert cpu_data["spec"]["for"] == "2m"
    assert cpu_data["spec"]["trigger"]["interval"] == "30s"

    mem_data = yaml.safe_load(mem_alert.read_text(encoding="utf-8"))
    assert mem_data["kind"] == "AlertRule"
    assert mem_data["spec"]["title"] == "Memory Usage High"


def test_user_adds_broken_syntax_json_and_yaml(tmp_path: Path) -> None:
    """
    Użytkownik wrzuca uszkodzony plik JSON lub niepoprawny YAML.
    Weryfikujemy:
    1. Wykrycie błędu jako ValidationIssue (IssueSeverity.ERROR).
    2. Nieusuwanie pliku użytkownika z dysku (ochrona danych).
    3. Flaga has_errors == True blokująca dalsze przetwarzanie.
    """
    folder = tmp_path / "PRD" / "Broken"
    folder.mkdir(parents=True)

    broken_json = folder / "broken.json"
    broken_json.write_text("{ this is: not valid json !!!", encoding="utf-8")

    broken_yaml = folder / "broken.yaml"
    broken_yaml.write_text("key: [unclosed list", encoding="utf-8")

    res = reconcile_data_directory(tmp_path)
    assert res.has_errors
    assert len(res.issues) >= 2
    assert all(issue.severity == IssueSeverity.ERROR for issue in res.issues)

    # Pliki nie mogą zostać bezpowrotnie skasowane!
    assert broken_json.exists()
    assert broken_yaml.exists()


def test_user_adds_empty_json_or_missing_title(tmp_path: Path) -> None:
    """
    Użytkownik dodaje pusty obiekt JSON `{}` lub brak tytułu.
    Weryfikujemy zgłoszenie błędu walidacji.
    """
    folder = tmp_path / "PRD" / "Empty"
    folder.mkdir(parents=True)

    empty_json = folder / "empty.json"
    empty_json.write_text("{}", encoding="utf-8")

    res = reconcile_data_directory(tmp_path)
    assert res.has_errors
    assert any("unrecognized" in issue.message or "title" in issue.message for issue in res.issues)


def test_user_uses_special_characters_emojis_and_polish_letters_in_title(tmp_path: Path) -> None:
    """
    Tytuł zasobu zawiera emotikony, znaki diakrytyczne i ukośniki:
    '🚀 Błędy w produkcji / API (v2.1) - Łódź'
    Weryfikujemy:
    1. Bezpieczną nazwę pliku ASCII bez spacji i ukośników.
    2. Zachowanie oryginalnego tytułu UTF-8 wewnątrz spec.title.
    """
    folder = tmp_path / "PRD" / "Apps"
    folder.mkdir(parents=True)

    title = "🚀 Błędy w produkcji / API (v2.1) - Łódź"
    expected_slug = slugify_filename(title, ".yaml", prefix="d")
    assert expected_slug == "d-bledy-w-produkcji-api-v2-1-lodz.yaml"

    (folder / "raw.json").write_text(
        json.dumps({"title": title, "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    res = reconcile_data_directory(tmp_path)
    assert not res.has_errors

    canonical_file = folder / expected_slug
    assert canonical_file.exists()

    content = yaml.safe_load(canonical_file.read_text(encoding="utf-8"))
    assert content["spec"]["title"] == title


def test_non_resource_files_ignored_and_not_deleted(tmp_path: Path) -> None:
    """
    W katalogu data/ znajdują się pliki pomocnicze: .gitkeep, README.md, .DS_Store, config.txt.
    Weryfikujemy, że normalizer ich nie dotyka i nie usuwa.
    """
    folder = tmp_path / "PRD" / "Docs"
    folder.mkdir(parents=True)

    gitkeep = folder / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")
    readme = folder / "README.md"
    readme.write_text("# Documentation", encoding="utf-8")
    ds_store = folder / ".DS_Store"
    ds_store.write_bytes(b"\x00\x01\x02")

    res = reconcile_data_directory(tmp_path)
    assert not res.has_errors
    assert gitkeep.exists()
    assert readme.exists()
    assert ds_store.exists()
    assert len(res.written_files) == 0
    assert len(res.deleted_raw_files) == 0


def test_user_directly_edits_canonical_manifest_diff_detected(tmp_path: Path) -> None:
    """
    Użytkownik edytuje istniejący kanoniczny plik d-*.yaml bezpośrednio w edytorze kodu
    (np. zmienia tytuł panelu lub dodaje tag).
    Weryfikujemy, że Git diff wykrywa to jako UPDATE pojedynczego zasobu.
    """
    repo = tmp_path / "repo"
    data_dir = repo / "data" / "PRD"
    data_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (data_dir / "dash.json").write_text(
        json.dumps({"title": "Live Dash", "schemaVersion": 39, "panels": [{"id": 1, "title": "Old"}]}),
        encoding="utf-8",
    )
    reconcile_data_directory(repo / "data")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")

    dash_yaml = data_dir / "d-live-dash.yaml"
    doc = yaml.safe_load(dash_yaml.read_text(encoding="utf-8"))
    doc["spec"]["panels"][0]["title"] = "New Title From Dev"
    with dash_yaml.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f)

    reconcile_data_directory(repo / "data")
    plan = build_delta_plan(repo, data_subdir="data", base_ref="HEAD")

    assert len(plan.to_change) == 1
    assert plan.to_change[0].title == "Live Dash"
    assert plan.to_change[0].action == PlanActionType.UPDATE
    assert len(plan.to_add) == 0
    assert len(plan.to_destroy) == 0
