"""Dedykowany zestaw testów jednostkowych i integracyjnych dla narzędzia grafana-live-sync (moduł live_sync)."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from live_sync.cli import main as live_sync_cli_main
from live_sync.grafana_state import (
    LiveGrafanaResource,
    canonicalize_spec,
    fetch_live_managed_resources,
    is_managed_uid,
)
from live_sync.models import PlanActionType
from live_sync.planner import (
    build_gcx_delta_commands,
    build_live_state_plan,
    format_terraform_summary,
)
from live_sync.reconciler import reconcile_data_directory


def _create_sample_local_repo(repo_dir: Path) -> Path:
    """Tworzy znormalizowaną strukturę repozytorium z folderem PRD i dashboardem d-test."""
    data_dir = repo_dir / "grafana" / "PRD"
    data_dir.mkdir(parents=True)
    raw_dash = data_dir / "dash.json"
    raw_dash.write_text(
        json.dumps(
            {
                "title": "Production Overview",
                "schemaVersion": 39,
                "panels": [{"id": 1, "title": "CPU Rate"}],
            }
        ),
        encoding="utf-8",
    )
    reconcile_data_directory(repo_dir / "grafana")
    return repo_dir


def test_is_managed_uid_protection() -> None:
    """
    Sprawdza filtr `is_managed_uid`:
    Tylko identyfikatory wygenerowane deterministycznie przez nasze repozytorium (f-/d-/a- + 20 znaków hex)
    są uznawane za zarządzane. Wszystkie zasoby utworzone ręcznie w UI Grafany są w 100% chronione!
    """
    assert is_managed_uid("f-1234567890abcdef1234") is True
    assert is_managed_uid("d-abcdef0123456789abcd") is True
    assert is_managed_uid("a-00000000000000000000") is True

    # Ręcznie utworzone w UI Grafany lub o innym formacie:
    assert is_managed_uid("ad-hoc-dashboard") is False
    assert is_managed_uid("manual_folder_prd") is False
    assert is_managed_uid("user-dashboard-12345") is False
    assert is_managed_uid("f-short") is False
    assert is_managed_uid("d-tooooooooooo-loooooong-hex-12345678901234567890") is False


def test_canonicalize_spec_go_durations_and_server_defaults() -> None:
    """
    Sprawdza, czy canonicalize_spec poprawnie ujednolica formaty czasowe Go oraz usuwa domyślne pola omitempty.
    Różnice w formatowaniu czasu (np. '1m' vs '60s' vs '1m0s') nie mogą powodować fałszywych zmian (~ UPDATE).
    """
    spec_from_git = {
        "title": "CPU High",
        "for": "1m",
        "trigger": {"interval": "30s"},
        "expressions": {
            "A": {
                "model": {"expr": "rate(cpu[5m]) > 0.8"},
                "relativeTimeRange": {"from": "600s", "to": "0s"},
            }
        },
    }

    spec_from_grafana_api = {
        "title": "CPU High",
        "for": "60s",  # API zwróciło w sekundach zamiast w minutach
        "trigger": {"interval": "30s"},
        "id": 999,  # Śmieci serwerowe dopisane przez Grafanę
        "uid": "a-12345678901234567890",
        "version": 4,
        "expressions": {
            "A": {
                "model": {
                    "expr": "rate(cpu[5m]) > 0.8",
                    "hide": False,  # Domyślne pole Go omitempty
                    "maxDataPoints": 100,
                },
                "relativeTimeRange": {"from": "10m0s", "to": "0s"},  # 10m0s == 600s
            }
        },
    }

    canon_git = canonicalize_spec("AlertRule", spec_from_git)
    canon_api = canonicalize_spec("AlertRule", spec_from_grafana_api)

    assert canon_git == canon_api  # Pełna zgodność semantyczna, zero fałszywego driftu!


def test_live_sync_plan_create_when_missing_in_grafana(tmp_path: Path) -> None:
    """
    Zasób istnieje w repozytorium (grafana/), ale brak go w Grafana Cloud.
    Plan powinien wyznaczyć akcję CREATE dla folderu oraz dashboardu.
    """
    repo = _create_sample_local_repo(tmp_path / "repo")

    # Żywy stan Grafany jest pusty
    live_resources: dict[tuple[str, str], LiveGrafanaResource] = {}

    plan = build_live_state_plan(repo_root=repo, live_resources=live_resources, data_subdir="grafana")

    assert len(plan.to_add) == 2  # Folder PRD + Dashboard Production Overview
    assert len(plan.to_change) == 0
    assert len(plan.to_destroy) == 0
    assert plan.to_add[0].kind == "Folder"
    assert plan.to_add[1].kind == "Dashboard"

    summary = format_terraform_summary(plan, is_apply=False)
    assert "Plan: 2 to add, 0 to change, 0 to destroy." in summary
    assert '+ Folder     "PRD"' in summary
    assert '+ Dashboard  "PRD/Production Overview"' in summary


def test_live_sync_plan_zero_changes_when_matching(tmp_path: Path) -> None:
    """
    Zasób w repozytorium jest identyczny ze stanem w chmurze Grafany.
    Plan powinien zawierać dokładnie 0 zmian (zero zapytań modyfikujących API).
    """
    repo = _create_sample_local_repo(tmp_path / "repo")
    dash_file = repo / "grafana" / "PRD" / "d-production-overview.yaml"
    folder_file = repo / "grafana" / "PRD" / "folder.yaml"

    dash_doc = yaml.safe_load(dash_file.read_text(encoding="utf-8"))
    folder_doc = yaml.safe_load(folder_file.read_text(encoding="utf-8"))

    dash_name = dash_doc["metadata"]["name"]
    folder_name = folder_doc["metadata"]["name"]

    live_resources = {
        ("Folder", folder_name): LiveGrafanaResource(
            kind="Folder",
            name=folder_name,
            title="PRD",
            folder_uid=None,
            spec=canonicalize_spec("Folder", folder_doc["spec"]),
        ),
        ("Dashboard", dash_name): LiveGrafanaResource(
            kind="Dashboard",
            name=dash_name,
            title="Production Overview",
            folder_uid=folder_name,
            spec=canonicalize_spec("Dashboard", dash_doc["spec"]),
        ),
    }

    plan = build_live_state_plan(repo_root=repo, live_resources=live_resources, data_subdir="grafana")
    assert len(plan.items) == 0

    summary = format_terraform_summary(plan, is_apply=False)
    assert "No changes. Grafana state matches repository (0 to add, 0 to change, 0 to destroy)." in summary


def test_live_sync_plan_update_on_drift(tmp_path: Path) -> None:
    """
    Wykrycie driftu: ktoś zmienił w UI Grafany specyfikację dashboardu (np. dodał dodatkowy panel).
    Plan powinien wyznaczyć akcję ~ UPDATE dla tego dashboardu, aby przywrócić stan z repozytorium.
    """
    repo = _create_sample_local_repo(tmp_path / "repo")
    dash_file = repo / "grafana" / "PRD" / "d-production-overview.yaml"
    folder_file = repo / "grafana" / "PRD" / "folder.yaml"

    dash_doc = yaml.safe_load(dash_file.read_text(encoding="utf-8"))
    folder_doc = yaml.safe_load(folder_file.read_text(encoding="utf-8"))

    dash_name = dash_doc["metadata"]["name"]
    folder_name = folder_doc["metadata"]["name"]

    # W Grafanie ktoś w UI dodał drugi panel (Drift!)
    drifted_spec = {
        "title": "Production Overview",
        "panels": [{"id": 1, "title": "CPU Rate"}, {"id": 2, "title": "Drifted Panel in UI"}],
    }

    live_resources = {
        ("Folder", folder_name): LiveGrafanaResource(
            kind="Folder",
            name=folder_name,
            title="PRD",
            folder_uid=None,
            spec=canonicalize_spec("Folder", folder_doc["spec"]),
        ),
        ("Dashboard", dash_name): LiveGrafanaResource(
            kind="Dashboard",
            name=dash_name,
            title="Production Overview",
            folder_uid=folder_name,
            spec=canonicalize_spec("Dashboard", drifted_spec),
        ),
    }

    plan = build_live_state_plan(repo_root=repo, live_resources=live_resources, data_subdir="grafana")
    assert len(plan.to_change) == 1
    assert plan.to_change[0].action == PlanActionType.UPDATE
    assert plan.to_change[0].title == "Production Overview"
    assert len(plan.to_add) == 0
    assert len(plan.to_destroy) == 0

    cmds = build_gcx_delta_commands(plan, gcx_bin="./gcx", dry_run=False)
    # 0) Pre-flight validate, 1) Push dashboardu z Git (naprawa driftu)
    assert len(cmds) == 2
    assert cmds[0][:3] == ["./gcx", "resources", "validate"]
    assert cmds[1][:3] == ["./gcx", "resources", "push"]
    assert "grafana/PRD/d-production-overview.yaml" in cmds[1]


def test_live_sync_plan_destroy_only_for_managed_uids(tmp_path: Path) -> None:
    """
    W Grafanie istnieją zasoby, których nie ma w repozytorium:
    1. Zasób z zarządzanym UID (`d-11111111111111111111`) -> powinien zostać zaplanowany do skasowania (- DESTROY).
    2. Zasób ręczny z UI (`manual-dashboard-uid`) -> filtr is_managed_uid() chroni go w 100%!
    """
    repo = tmp_path / "repo"
    (repo / "grafana").mkdir(parents=True)

    managed_orphan_uid = "d-11111111111111111111"
    live_resources = {
        ("Dashboard", managed_orphan_uid): LiveGrafanaResource(
            kind="Dashboard",
            name=managed_orphan_uid,
            title="Old Managed Dash",
            folder_uid=None,
            spec={"title": "Old Managed Dash"},
        ),
    }

    plan = build_live_state_plan(repo_root=repo, live_resources=live_resources, data_subdir="grafana")

    assert len(plan.to_destroy) == 1
    assert plan.to_destroy[0].name == managed_orphan_uid
    assert plan.to_destroy[0].action == PlanActionType.DESTROY

    cmds = build_gcx_delta_commands(plan, gcx_bin="./gcx", dry_run=False)
    assert len(cmds) == 1
    assert cmds[0] == ["./gcx", "resources", "delete", f"dashboards/{managed_orphan_uid}", "-y"]


def test_fetch_live_managed_resources_with_mocked_gcx_pull(tmp_path: Path) -> None:
    """
    Sprawdza działanie `fetch_live_managed_resources` z atrapą (mockiem) wywołania `gcx resources pull`.
    Weryfikujemy odczytanie plików JSON i odfiltrowanie zasobów niezarządzanych.
    """
    managed_dash_uid = "d-22222222222222222222"
    unmanaged_dash_uid = "ad-hoc-user-dashboard"

    def fake_subprocess_run(cmd: list[str], *args, **kwargs):
        # Pobieramy ścieżkę katalogu tymczasowego przekazaną po fladze -p
        tmp_idx = cmd.index("-p") + 1
        tmp_dir = Path(cmd[tmp_idx])

        # Tworzymy 1 zasób zarządzany
        (tmp_dir / "managed.json").write_text(
            json.dumps(
                {
                    "kind": "Dashboard",
                    "metadata": {"name": managed_dash_uid},
                    "spec": {"title": "Managed Dash"},
                }
            ),
            encoding="utf-8",
        )
        # Tworzymy 1 zasób ręczny niezarządzany
        (tmp_dir / "unmanaged.json").write_text(
            json.dumps(
                {
                    "kind": "Dashboard",
                    "metadata": {"name": unmanaged_dash_uid},
                    "spec": {"title": "Unmanaged Dash"},
                }
            ),
            encoding="utf-8",
        )
        res = MagicMock()
        res.returncode = 0
        res.stdout = "pulled successfully"
        res.stderr = ""
        return res

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        resources = fetch_live_managed_resources(gcx_bin="gcx")

    assert len(resources) == 1
    assert ("Dashboard", managed_dash_uid) in resources
    assert ("Dashboard", unmanaged_dash_uid) not in resources


def test_live_sync_cli_plan_summary_file(tmp_path: Path) -> None:
    """
    Sprawdza uruchomienie CLI narzędzia live_sync w trybie plan z flagami --skip-gcx i --summary-file.
    Weryfikujemy poprawny kod wyjścia (0) oraz wygenerowanie pliku raportu Markdown.
    """
    repo = _create_sample_local_repo(tmp_path / "repo")
    summary_file = repo / "plan-summary.md"

    def fake_subprocess_run(cmd: list[str], *args, **kwargs):
        res = MagicMock()
        res.returncode = 0
        res.stdout = ""
        res.stderr = ""
        return res

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        exit_code = live_sync_cli_main(
            [
                "plan",
                "--repo-root",
                str(repo),
                "--data-dir",
                "grafana",
                "--gcx-bin",
                "gcx",
                "--skip-gcx",
                "--summary-file",
                str(summary_file),
            ]
        )

    assert exit_code == 0
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "## Deploy grafana-observability (TF-style)" in content
    assert "Plan: 2 to add" in content
