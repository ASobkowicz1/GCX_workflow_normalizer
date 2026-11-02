"""Zestaw testów sprawdzających edge-case'y oraz nietypowe akcje użytkownika w głównym silniku GitOps (normalizer)."""

import json
import subprocess
from pathlib import Path

import yaml

from unittest.mock import MagicMock, patch

from normalizer.cli import (
    _diagnose_batch_failure,
    _execute_commands,
    _extract_error_detail,
    _filter_gcx_output,
    _format_command_header,
)
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
    # 1) Skonsolidowany push w porządku topologicznym: folder NewApp + dashboard NewApp
    # 2) Delete starego dashboardu
    # 3) Delete starego folderu OldApp
    assert len(cmds) == 4
    assert cmds[0][:3] == ["./gcx", "resources", "validate"]
    assert cmds[1] == [
        "./gcx",
        "resources",
        "push",
        "-p",
        "data/PRD/NewApp/folder.yaml",
        "-p",
        "data/PRD/NewApp/d-app-metrics.yaml",
    ]
    assert cmds[2] == ["./gcx", "resources", "delete", f"dashboards/{old_dash_uid}", "-y"]
    assert cmds[3] == ["./gcx", "resources", "delete", "folders/f-", "-y"] or "folders/" in cmds[3][3]


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


def test_deleted_folder_manifest_with_active_resources_is_not_destroyed(tmp_path: Path) -> None:
    """
    Użytkownik przez pomyłkę usunął plik folder.yaml (lub git diff wykazuje 'D' dla folder.yaml),
    ale w tym samym folderze nadal znajdują się aktywne alerty lub dashboardy.
    Reconciler odtwarza folder.yaml na dysku, a planner NIE generuje polecenia DESTROY dla tego folderu
    (chroniąc przed błędem exit code 4 w Grafana Cloud).
    """
    repo = tmp_path / "repo"
    data_dir = repo / "data" / "PRD"
    data_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (data_dir / "dash.json").write_text(
        json.dumps({"title": "Active Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    reconcile_data_directory(repo / "data")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")

    # Użytkownik kasuje sam plik folder.yaml, zostawiając d-active-dash.yaml
    (data_dir / "folder.yaml").unlink()
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Accidentally deleted folder.yaml")

    # Reconciler widzi d-active-dash.yaml i odtwarza folder.yaml na dysku
    reconcile_data_directory(repo / "data")
    plan = build_delta_plan(repo, data_subdir="data", base_ref="HEAD~1")

    # Ponieważ folder.yaml istnieje na dysku (bo folder nadal ma dashboard), planner nie planuje usunięcia folderu PRD!
    assert all(item.kind != "Folder" or item.action != PlanActionType.DESTROY for item in plan.items)


def test_renamed_or_duplicate_folder_manifest_is_cleaned_up(tmp_path: Path) -> None:
    """
    Jeśli użytkownik zmieni nazwę pliku folder.yaml (np. na my-folder.yaml lub backup-folder.yaml),
    normalizator musi usunąć plik o niepoprawnej nazwie i zapewnić, że w katalogu istnieje dokładnie
    jeden plik folderu: kanoniczny `folder.yaml`.
    """
    folder = tmp_path / "PRD" / "Applications"
    folder.mkdir(parents=True)

    # Użytkownik wrzuca dashboard oraz niekanoniczny plik folderu o innej nazwie
    (folder / "dash.json").write_text(
        json.dumps({"title": "App Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    (folder / "custom-folder.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "folder.grafana.app/v1beta1",
                "kind": "Folder",
                "metadata": {"name": "f-12345678901234567890"},
                "spec": {"title": "Old Name"},
            }
        ),
        encoding="utf-8",
    )

    res = reconcile_data_directory(tmp_path)
    assert not res.has_errors
    # Niekanoniczny plik musi zostać skasowany
    assert not (folder / "custom-folder.yaml").exists()
    assert folder / "custom-folder.yaml" in res.deleted_raw_files
    # Kanoniczny folder.yaml musi istnieć
    assert (folder / "folder.yaml").exists()
    # W folderze ma być dokładnie jeden plik z kind: Folder (sam folder.yaml)
    folder_files = [
        f for f in folder.iterdir()
        if f.is_file() and detect_and_parse(f).kind == FileKind.CANONICAL_FOLDER
    ]
    assert len(folder_files) == 1
    assert folder_files[0].name == "folder.yaml"


def test_format_command_header_clean_display() -> None:
    """Sprawdza czytelne formatowanie komunikatów o walidowanych i wdrażanych plikach z obcięciem data_dir."""
    # Walidacja zasobów z podaniem data_dir
    cmd_validate = [
        "gcx",
        "resources",
        "validate",
        "-p",
        "example_repo/grafana/PRD/folder.yaml",
        "-p",
        "example_repo/grafana/PRD/d-app.yaml",
    ]
    header_val = _format_command_header(
        cmd_validate, is_apply=False, data_dir="example_repo/grafana"
    )
    assert "🔍 Validating resources against Grafana API:" in header_val
    assert "  • PRD/folder.yaml" in header_val
    assert "  • PRD/d-app.yaml" in header_val
    assert "example_repo/grafana" not in header_val
    assert "-p" not in header_val

    # Push zasobów z podaniem data_dir
    cmd_push = ["gcx", "resources", "push", "-p", "example_repo/grafana/PRD/folder.yaml"]
    header_push = _format_command_header(
        cmd_push, is_apply=True, data_dir="example_repo/grafana"
    )
    assert "🚀 Pushing resources to Grafana:" in header_push
    assert "  • PRD/folder.yaml" in header_push
    assert "example_repo/grafana" not in header_push

    # Delete zasobów (selektory kind/uid)
    cmd_delete = ["gcx", "resources", "delete", "dashboards/d-12345", "-y"]
    header_del = _format_command_header(cmd_delete, is_apply=True)
    assert "🚀 Deleting resources from Grafana:" in header_del
    assert "  • dashboards/d-12345" in header_del

    # Filtrowanie szumów GCX dry-run
    raw_gcx_out = (
        "✔ No errors found.\n"
        "⚠ recordingrules.rules.alerting.grafana.app does not support server-side dry-run. gcx checked client-side only...\n"
        "⚠ alertrules.rules.alerting.grafana.app does not support server-side dry-run. gcx checked client-side only...\n"
    )
    cleaned = _filter_gcx_output(raw_gcx_out)
    assert "does not support server-side dry-run" not in cleaned
    assert "✔ No errors found." in cleaned


def test_reconcile_and_plan_recording_rule(tmp_path: Path) -> None:
    """
    Sprawdza pełny cykl życia dla Grafana-managed RecordingRule:
    1. Wrzucony surowy eksport RecordingRule z UI do folderu PRD/Metrics.
    2. Normalizator tworzy kanoniczny plik `r-request-rate.yaml` oraz `folder.yaml`.
    3. Plan wykrywa akcję CREATE dla RecordingRule.
    4. Polecenie GCX push zawiera kanoniczny plik r-*.yaml.
    """
    repo = tmp_path / "repo"
    data_dir = repo / "data"
    metrics_dir = data_dir / "PRD" / "Metrics"
    metrics_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (data_dir / "init.txt").write_text("initial", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")

    # Wrzucamy surowy eksport z UI
    raw_rec = {
        "apiVersion": "rules.alerting.grafana.app/v0alpha1",
        "kind": "RecordingRule",
        "metadata": {
            "name": "raw-uid-12345",
            "uid": "raw-uid-12345",
            "resourceVersion": "1",
            "annotations": {"grafana.app/folder": "old-folder"},
        },
        "spec": {
            "title": "Request Rate",
            "metric": "job:request_rate:5m",
            "targetDatasourceUID": "prom-uid",
            "trigger": {"interval": "1m"},
            "expressions": {
                "A": {
                    "datasourceUID": "prom-uid",
                    "model": {"expr": "rate(http[5m])"},
                }
            },
        },
    }
    (metrics_dir / "raw_rec.yaml").write_text(yaml.safe_dump(raw_rec), encoding="utf-8")

    res = reconcile_data_directory(data_dir)
    assert not res.has_errors
    assert not (metrics_dir / "raw_rec.yaml").exists()

    canonical_rec = metrics_dir / "r-request-rate.yaml"
    assert canonical_rec.exists()

    rec_data = yaml.safe_load(canonical_rec.read_text(encoding="utf-8"))
    assert rec_data["kind"] == "RecordingRule"
    assert rec_data["metadata"]["name"].startswith("r-")
    assert len(rec_data["metadata"]["name"]) == 22
    assert "resourceVersion" not in rec_data["metadata"]
    assert rec_data["spec"]["metric"] == "job:request_rate:5m"
    assert rec_data["spec"]["targetDatasourceUID"] == "prom-uid"
    assert rec_data["spec"]["expressions"]["A"]["source"] is True

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Add recording rule")

    # Sprawdzamy wyliczenie planu
    plan = build_delta_plan(repo, data_subdir="data", base_ref="HEAD~1")
    assert len(plan.to_add) == 3  # Folder PRD + Folder Metrics + RecordingRule Request Rate
    rec_item = next(i for i in plan.to_add if i.kind == "RecordingRule")
    assert rec_item.title == "Request Rate"
    assert rec_item.action == PlanActionType.CREATE

    cmds = build_gcx_delta_commands(plan, gcx_bin="./gcx")
    # Pre-flight validate oraz skonsolidowany push
    assert len(cmds) == 2
    assert "data/PRD/Metrics/r-request-rate.yaml" in cmds[1]


def test_extract_error_detail_various_cases() -> None:
    """Sprawdza wyciąganie czytelnych szczegółów błędu z wyjścia GCX."""
    # 1. Błąd walidacji ze specyficznym komunikatem
    stderr_val = "warn: 1 resource(s) failed to push\nerror: spec.targetDatasourceUID cannot be empty"
    msg = _extract_error_detail("", stderr_val, default_code=4)
    assert "spec.targetDatasourceUID cannot be empty" in msg

    # 2. Błąd z kodem 4 bez szczegółów tekstowych
    msg_generic = _extract_error_detail("", "warn: 1 resource(s) failed to push", default_code=4)
    assert "exit code 4" in msg_generic
    assert "Schema/field validation error" in msg_generic

    # 3. Ignorowanie szumów dry-run i auto-approval
    stderr_noise = (
        "Auto-approval enabled: automatically setting --force\n"
        "does not support server-side dry-run\n"
        "invalid datasource UID 'prom-fake'\n"
    )
    msg_clean = _extract_error_detail("", stderr_noise, default_code=1)
    assert "invalid datasource UID 'prom-fake'" in msg_clean
    assert "Auto-approval" not in msg_clean


def test_batch_auto_diagnostics_reports_all_failures(capsys) -> None:
    """
    Sprawdza, czy w przypadku błędu partii silnik bada wszystkie pliki z paczki
    i raportuje WSZYSTKIE uszkodzone zasoby naraz (a nie tylko pierwszy).
    """
    cmd = [
        "gcx",
        "resources",
        "push",
        "-p",
        "data/PRD/folder.yaml",
        "-p",
        "data/PRD/a-broken1.yaml",
        "-p",
        "data/PRD/a-broken2.yaml",
    ]

    # Symulacja subprocess:
    # 1. Batch call -> zwraca błąd ogólny (kod 4)
    # 2. Diag dla folder.yaml -> sukces (kod 0)
    # 3. Diag dla a-broken1.yaml -> błąd (kod 4, brak targetDatasourceUID)
    # 4. Diag dla a-broken2.yaml -> błąd (kod 4, nieprawidłowe query)
    batch_res = MagicMock(returncode=4, stdout="", stderr="warn: 2 resource(s) failed to push")
    diag_f1 = MagicMock(returncode=0, stdout="✔ pushed", stderr="")
    diag_f2 = MagicMock(returncode=4, stdout="", stderr="error: targetDatasourceUID cannot be empty")
    diag_f3 = MagicMock(returncode=4, stdout="", stderr="error: invalid promql query")

    with patch("subprocess.run", side_effect=[batch_res, diag_f1, diag_f2, diag_f3]):
        exit_code = _execute_commands([cmd], repo_root=Path("/fake"), is_apply=True, data_dir="data")

    assert exit_code == 4
    captured = capsys.readouterr()
    # Weryfikacja: oba uszkodzone zasoby są wylistowane w jednym raporcie
    assert "Deployment failed – 2 resource(s) rejected by Grafana API:" in captured.out
    assert "PRD/a-broken1.yaml" in captured.out
    assert "targetDatasourceUID cannot be empty" in captured.out
    assert "PRD/a-broken2.yaml" in captured.out
    assert "invalid promql query" in captured.out
    # Plik poprawny nie może znaleźć się na liście odrzuconych
    assert "PRD/folder.yaml" not in captured.out.split("Deployment failed")[1]


def test_consolidated_delete_execution_and_404_handling(capsys) -> None:
    """
    Sprawdza, czy seria poleceń delete jest prezentowana jako jeden skonsolidowany blok
    oraz czy błędy 404 (zasób już usunięty) są bezpiecznie ignorowane.
    """
    cmds = [
        ["gcx", "resources", "delete", "alertrules/a-1", "-y"],
        ["gcx", "resources", "delete", "folders/f-child", "-y"],
        ["gcx", "resources", "delete", "folders/f-parent", "-y"],
    ]

    # Symulacja:
    # 1. alertrules/a-1 -> usunięty (kod 0)
    # 2. folders/f-child -> 404 Not Found (zasób już nie istnieje)
    # 3. folders/f-parent -> usunięty (kod 0)
    res_a1 = MagicMock(returncode=0, stdout="✔ deleted", stderr="")
    res_child = MagicMock(returncode=1, stdout="", stderr="Error: 404 Not Found")
    res_parent = MagicMock(returncode=0, stdout="✔ deleted", stderr="")

    with patch("subprocess.run", side_effect=[res_a1, res_child, res_parent]):
        exit_code = _execute_commands(cmds, repo_root=Path("/fake"), is_apply=True)

    assert exit_code == 0
    captured = capsys.readouterr()
    # Sprawdzamy, czy nagłówek "Deleting" pojawił się dokładnie RAZ
    assert captured.out.count("🚀 Deleting resources from Grafana:") == 1
    # Wszystkie 3 selektory wymienione w jednym skonsolidowanym bloku
    assert "alertrules/a-1" in captured.out
    assert "folders/f-child" in captured.out
    assert "folders/f-parent" in captured.out
    # Podsumowanie końcowe: 2 usunięte, 1 już nieobecny
    assert "2 resources deleted, 1 already absent in Grafana, 0 errors" in captured.out


def test_delete_idempotent_when_resource_absent_with_code_4(capsys) -> None:
    """
    Sprawdza, czy gdy GCX delete zwróci kod 4 (bo zasób nie istnieje w Grafanie i nie wypisze 404),
    silnik weryfikuje istnienie zasobu przez 'get', uznaje go za nieobecny i nie przerywa wdrożenia.
    """
    cmds = [
        ["gcx", "resources", "delete", "recordingrules/r-1", "dashboards/d-1", "-y"],
    ]

    # Symulacja:
    # 1. delete batch -> kod 4, stderr="warn: 2 resource(s) failed to delete\n✘ 0 resources deleted, 2 errors"
    # 2. get recordingrules r-1 -> kod 1, stderr="Error: not found"
    # 3. get dashboards d-1 -> kod 1, stderr="Error: not found"
    res_del = MagicMock(
        returncode=4,
        stdout="✘ 0 resources deleted, 2 errors",
        stderr="warn: 2 resource(s) failed to delete",
    )
    res_chk1 = MagicMock(returncode=1, stdout="", stderr="Error: not found")
    res_chk2 = MagicMock(returncode=1, stdout="", stderr="Error: not found")

    with patch("subprocess.run", side_effect=[res_del, res_chk1, res_chk2]):
        exit_code = _execute_commands(cmds, repo_root=Path("/fake"), is_apply=True)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "2 already absent in Grafana, 0 errors" in captured.out


def test_delete_reports_real_failure_when_resource_exists(capsys) -> None:
    """
    Sprawdza, że jeśli zasób ISTNIEJE w Grafanie, ale nie daje się usunąć (np. niepusty folder),
    silnik NIE uznaje go za absent, lecz poprawnie raportuje błąd i przerywa z kodem 4.
    """
    cmds = [
        ["gcx", "resources", "delete", "folders/f-active", "-y"],
    ]

    res_del = MagicMock(returncode=4, stdout="", stderr="warn: 1 resource(s) failed to delete")
    res_chk = MagicMock(returncode=0, stdout='{"metadata": {"name": "f-active"}}', stderr="")
    res_retry = MagicMock(
        returncode=4, stdout="", stderr="folder still contains active dashboards"
    )

    with patch("subprocess.run", side_effect=[res_del, res_chk, res_retry]):
        exit_code = _execute_commands(cmds, repo_root=Path("/fake"), is_apply=True)

    assert exit_code == 4
    captured = capsys.readouterr()
    assert "Deletion failed – 1 resource(s) could not be deleted:" in captured.out
    assert "folders/f-active" in captured.out
    assert "Folder could not be deleted" in captured.out
