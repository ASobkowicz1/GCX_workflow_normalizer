import json
import subprocess
from pathlib import Path

from normalizer.models import PlanActionType
from normalizer.planner import (
    build_delta_plan,
    build_gcx_delta_commands,
    format_terraform_summary,
)
from normalizer.reconciler import reconcile_data_directory


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_full_git_lifecycle_create_update_destroy(tmp_path: Path) -> None:
    """
    Testuje cały cykl życia z git diff:
    1. Inicjalizacja repo i commit początkowy (Create folder + dashboard).
    2. Aktualizacja jednego dashboardu, dodanie drugiego i usunięcie starego (Create + Update + Destroy).
    3. Sprawdzenie wygenerowanego planu Terraform-style oraz kolejności komend GCX (Delta-only!).
    """
    repo = tmp_path / "repo"
    data_dir = repo / "data"
    data_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    # KROK 1: User wrzuca dwa dashboardy w strukturze 2-poziomowej: PRD / Azure
    azure_dir = data_dir / "PRD" / "Azure"
    azure_dir.mkdir(parents=True)
    (azure_dir / "vm-1.json").write_text(
        json.dumps({"title": "VM Overview", "schemaVersion": 39, "panels": [{"id": 1}]}),
        encoding="utf-8",
    )
    (azure_dir / "old-dash.json").write_text(
        json.dumps({"title": "Old Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )

    reconcile_data_directory(data_dir)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial normalized state")

    # KROK 2: Na nowym branchu/commicie user:
    # - aktualizuje "VM Overview" (nowy surowy eksport)
    # - usuwa "d-old-dash.yaml"
    # - dodaje nowy dashboard "Network"
    (azure_dir / "VM Overview-export2.json").write_text(
        json.dumps({"title": "VM Overview", "schemaVersion": 39, "panels": [{"id": 1}, {"id": 2}]}),
        encoding="utf-8",
    )
    (azure_dir / "d-old-dash.yaml").unlink()
    (azure_dir / "network-raw.json").write_text(
        json.dumps({"title": "Network", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )

    reconcile_data_directory(data_dir)

    # KROK 3: Liczymy deltę względem HEAD (dokładnie dotknięte zasoby!)
    plan = build_delta_plan(repo, data_subdir="data", base_ref="HEAD")

    assert len(plan.to_add) == 1
    assert plan.to_add[0].title == "Network"
    assert plan.to_add[0].action == PlanActionType.CREATE

    assert len(plan.to_change) == 1
    assert plan.to_change[0].title == "VM Overview"
    assert plan.to_change[0].action == PlanActionType.UPDATE

    assert len(plan.to_destroy) == 1
    assert plan.to_destroy[0].title == "Old Dash"
    assert plan.to_destroy[0].action == PlanActionType.DESTROY
    assert plan.to_destroy[0].name.startswith("d-")

    # Zauważ: foldery PRD i Azure NIE zmieniły się w tym PR, więc NIE ma ich w delcie!
    assert all(item.kind != "Folder" for item in plan.items)

    summary = format_terraform_summary(plan, is_apply=False)
    assert "## Deploy grafana-observability (TF-style)" in summary
    assert "Plan: 1 to add, 1 to change, 1 to destroy." in summary
    assert '+ Dashboard  "PRD/Azure/Network"' in summary
    assert '~ Dashboard  "PRD/Azure/VM Overview"' in summary
    assert '- Dashboard  "PRD/Azure/Old Dash"' in summary

    summary_apply = format_terraform_summary(plan, is_apply=True)
    assert "Apply: 1 to add, 1 to change, 1 to destroy." in summary_apply

    # Sprawdźmy wygenerowane komendy GCX w trybie dry_run (validate dla push_items) oraz deploy (validate + push + delete)
    cmds_dry = build_gcx_delta_commands(plan, gcx_bin="./gcx", dry_run=True)
    assert len(cmds_dry) == 1
    assert cmds_dry[0][:3] == ["./gcx", "resources", "validate"]

    # Sprawdźmy wygenerowane komendy GCX w trybie deploy (1: pre-flight validate, 2: push, 3: delete)
    cmds = build_gcx_delta_commands(plan, gcx_bin="./gcx", dry_run=False)
    assert len(cmds) == 3
    assert cmds[0][:3] == ["./gcx", "resources", "validate"]
    assert cmds[1][:3] == ["./gcx", "resources", "push"]
    assert "data/PRD/Azure/d-network.yaml" in cmds[1]
    assert "data/PRD/Azure/d-vm-overview.yaml" in cmds[1]
    assert cmds[2] == [
        "./gcx",
        "resources",
        "delete",
        f"dashboards/{plan.to_destroy[0].name}",
        "-y",
    ]


def test_folder_ordering_in_gcx_commands(tmp_path: Path) -> None:
    """Sprawdza, czy nowe foldery są wypychane poziomami L1 -> L2, a kasowane od L2 -> L1."""
    repo = tmp_path / "repo"
    data_dir = repo / "data"
    data_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    # Puste repo jako pierwszy commit
    (data_dir / "readme.md").write_text("init", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Empty init")

    # Dodajemy zasób na 2 poziomie (powstaną 2 nowe foldery: L1 "PRD" i L2 "PRD/Usage")
    usage_dir = data_dir / "PRD" / "Usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "dash.json").write_text(
        json.dumps({"title": "My Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    reconcile_data_directory(data_dir)

    plan_create = build_delta_plan(repo, data_subdir="data", base_ref="HEAD")
    cmds_create = build_gcx_delta_commands(plan_create, gcx_bin="./gcx")

    # 4 komendy: 0) Pre-flight validate, 1) Push folderu L1 (PRD), 2) Push folderu L2 (PRD/Usage), 3) Push dashboardu
    assert len(cmds_create) == 4
    assert cmds_create[0][0:3] == ["./gcx", "resources", "validate"]
    assert cmds_create[1] == ["./gcx", "resources", "push", "-p", "data/PRD/folder.yaml"]
    assert cmds_create[2] == ["./gcx", "resources", "push", "-p", "data/PRD/Usage/folder.yaml"]
    assert cmds_create[3] == ["./gcx", "resources", "push", "-p", "data/PRD/Usage/d-my-dash.yaml"]


def test_zero_commit_sha_from_github_actions_new_branch(tmp_path: Path) -> None:
    """Sprawdza obsługę base_ref='0'*40 (gdy GitHub Actions uruchamia deploy na nowym branchu)."""
    repo = tmp_path / "repo"
    data_dir = repo / "data" / "PRD"
    data_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (data_dir / "dash.json").write_text(
        json.dumps({"title": "First Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    reconcile_data_directory(repo / "data")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "First commit on branch")

    plan = build_delta_plan(repo, data_subdir="data", base_ref="0" * 40, target_ref="HEAD")
    assert len(plan.to_add) == 2  # Folder PRD + Dashboard First Dash
    assert len(plan.to_change) == 0
    assert len(plan.to_destroy) == 0


def test_full_push_pushes_all_files(tmp_path: Path) -> None:
    """Sprawdza, czy flaga full_push=True traktuje wszystkie pliki z repo jako Create (bez kasowania czegokolwiek)."""
    repo = tmp_path / "repo"
    data_dir = repo / "data" / "PRD"
    data_dir.mkdir(parents=True)

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (data_dir / "dash.json").write_text(
        json.dumps({"title": "Sync Dash", "schemaVersion": 39, "panels": []}),
        encoding="utf-8",
    )
    reconcile_data_directory(repo / "data")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Committed dash")

    # Gdy stan na dysku odpowiada HEAD, zwykły diff zwraca pusty plan:
    plan_normal = build_delta_plan(repo, data_subdir="data", base_ref="HEAD")
    assert len(plan_normal.items) == 0

    # Z flagą full_push plan obejmuje wszystkie zasoby niezależnie od braku zmian:
    plan_sync = build_delta_plan(repo, data_subdir="data", base_ref="HEAD", full_push=True)
    assert len(plan_sync.to_add) == 2  # Folder PRD + Dashboard Sync Dash
    assert len(plan_sync.to_change) == 0
    assert len(plan_sync.to_destroy) == 0


