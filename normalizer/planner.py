"""Analiza różnic Git (git diff), budowanie planu w stylu Terraform oraz generowanie komend GCX dla delty."""

import subprocess
from pathlib import Path
from typing import Any

import yaml

from normalizer.models import (
    ExecutionPlan,
    FileKind,
    PlanActionType,
    PlanItem,
    ValidationIssue,
)
from normalizer.parser import detect_and_parse
from normalizer.paths import get_canonical_folder_path, get_folder_depth

# Stały identyfikator pustego drzewa w Git (używany gdy github.event.before to 40 zer lub brak rodzica)
GIT_EMPTY_TREE_HASH = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_COMMIT_SHA = "0" * 40


def _run_git(repo_root: Path, args: list[str]) -> str:
    """
    Uruchamia polecenie git w podanym katalogu repozytorium i zwraca standardowe wyjście.
    Flaga `-c core.quotepath=false` gwarantuje poprawne ścieżki UTF-8 dla polskich znaków i spacji.
    """
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _resolve_effective_base_ref(
    repo_root: Path,
    base_ref: str,
    target_ref: str | None,
) -> str:
    """
    Wyznacza punkt odniesienia do porównania w Git:
    1. Jeśli `base_ref` to 40 zer (nowy branch w GitHub Actions), zwraca `GIT_EMPTY_TREE_HASH`.
    2. W trybie deploy (gdy target_ref jest podany) lub gdy base_ref to HEAD, zwraca bezpośrednio base_ref.
    3. W trybie plan gdy podano branch bazowy (np. origin/prd), wyznacza wspólnego przodka (merge-base).
    """
    if base_ref == ZERO_COMMIT_SHA:
        return GIT_EMPTY_TREE_HASH

    if target_ref is not None or base_ref == "HEAD":
        return base_ref

    try:
        merge_base = _run_git(repo_root, ["merge-base", base_ref, "HEAD"]).strip()
        if merge_base:
            return merge_base
    except subprocess.CalledProcessError:
        pass

    return base_ref


def _extract_resource_metadata(data: dict[str, Any], fallback_path: Path) -> tuple[str, str, str]:
    """Wyciąga trójkę (kind, metadata.name, spec.title) z kanonicznego słownika zasobu."""
    kind = str(data.get("kind", "Unknown"))
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    name = str(meta.get("name", fallback_path.stem))
    spec = data.get("spec") if isinstance(data.get("spec"), dict) else {}
    title = str(spec.get("title", fallback_path.stem))
    return kind, name, title


def build_delta_plan(
    repo_root: Path,
    data_subdir: str = "data",
    base_ref: str = "HEAD",
    target_ref: str | None = None,
    issues: list[ValidationIssue] | None = None,
    full_push: bool = False,
) -> ExecutionPlan:
    """
    Buduje plan zmian (Create / Update / Destroy) na podstawie `git diff`:
    - Gdy `full_push=True`: porównuje stan z pustym drzewem Git (`GIT_EMPTY_TREE_HASH`),
      co powoduje wypchnięcie wszystkich zasobów z repozytorium (np. z workflow_dispatch).
    - Gdy `target_ref` jest None: porównuje `base_ref` z plikami na dysku (working tree + untracked).
    - Gdy `target_ref` jest podany (np. po merge'u): porównuje dwa commity `base_ref..target_ref`.
    """
    repo_root = repo_root.resolve()
    data_root = (repo_root / data_subdir).resolve()
    plan = ExecutionPlan(issues=list(issues) if issues else [])

    effective_base = (
        GIT_EMPTY_TREE_HASH
        if full_push
        else _resolve_effective_base_ref(repo_root, base_ref, target_ref)
    )

    # 1. Pobranie zmian śledzonych przez Git względem effective_base
    diff_args = ["diff", "--name-status", "--no-renames", effective_base]
    if target_ref:
        diff_args.append(target_ref)
    diff_args.extend(["--", data_subdir])

    raw_diff = _run_git(repo_root, diff_args)
    file_statuses: dict[str, str] = {}

    for line in raw_diff.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", maxsplit=1)
        if len(parts) == 2:
            status, rel_repo_path = parts[0].strip(), parts[1].strip()
            if rel_repo_path.startswith('"') and rel_repo_path.endswith('"'):
                rel_repo_path = rel_repo_path[1:-1]
            file_statuses[rel_repo_path] = status[0]  # 'A', 'M', 'D'

    # 2. Doliczenie nowych, nieśledzonych plików (untracked) w trybie porównania z dyskiem
    if target_ref is None:
        untracked_out = _run_git(
            repo_root,
            ["ls-files", "--others", "--exclude-standard", "--", data_subdir],
        )
        for line in untracked_out.splitlines():
            rel_repo_path = line.strip()
            if rel_repo_path.startswith('"') and rel_repo_path.endswith('"'):
                rel_repo_path = rel_repo_path[1:-1]
            if rel_repo_path:
                file_statuses[rel_repo_path] = "A"

    # 3. Klasyfikacja każdego zmienionego pliku w data/
    for rel_repo_path, status in sorted(file_statuses.items()):
        abs_path = (repo_root / rel_repo_path).resolve()
        canonical_folder = get_canonical_folder_path(data_root, abs_path)
        if canonical_folder is None:
            continue
        depth = get_folder_depth(canonical_folder)

        if status in ("A", "M"):
            if not abs_path.exists():
                continue
            parsed = detect_and_parse(abs_path)
            if (
                parsed.kind
                not in (
                    FileKind.CANONICAL_FOLDER,
                    FileKind.CANONICAL_DASHBOARD,
                    FileKind.CANONICAL_ALERTRULE,
                    FileKind.CANONICAL_RECORDINGRULE,
                )
                or not parsed.data
            ):
                continue

            kind, name, title = _extract_resource_metadata(parsed.data, abs_path)
            action = PlanActionType.CREATE if status == "A" else PlanActionType.UPDATE
            plan.items.append(
                PlanItem(
                    action=action,
                    kind=kind,
                    name=name,
                    title=title,
                    canonical_folder=canonical_folder,
                    repo_path=Path(rel_repo_path),
                    depth=depth,
                )
            )

        elif status == "D":
            # Jeśli plik nadal istnieje na dysku (np. został odtworzony przez reconciler, bo folder nadal zawiera zasoby),
            # nie planujemy jego usunięcia w Grafanie.
            if abs_path.exists():
                continue

            # Dla usuniętego pliku odczytujemy jego ostatnią zawartość z Gita za pomocą `git show`
            if abs_path.suffix.lower() not in (".yaml", ".yml"):
                continue
            try:
                old_content = _run_git(repo_root, ["show", f"{effective_base}:{rel_repo_path}"])
                old_data = yaml.safe_load(old_content)
            except Exception:
                continue

            if not isinstance(old_data, dict):
                continue
            api_version = str(old_data.get("apiVersion", ""))
            if not any(
                api_version.startswith(prefix)
                for prefix in (
                    "folder.grafana.app/",
                    "dashboard.grafana.app/",
                    "rules.alerting.grafana.app/",
                )
            ):
                continue

            kind, name, title = _extract_resource_metadata(old_data, abs_path)
            plan.items.append(
                PlanItem(
                    action=PlanActionType.DESTROY,
                    kind=kind,
                    name=name,
                    title=title,
                    canonical_folder=canonical_folder,
                    repo_path=Path(rel_repo_path),
                    depth=depth,
                )
            )

    # 4. Ochrona przed jednoczesnym CREATE + DESTROY tego samego UID (np. gdy zmieniła się tylko nazwa pliku na dysku):
    # Jeśli dany (kind, name) jest obecny w CREATE lub UPDATE, nie możemy go skasować w DESTROY!
    active_keys = {
        (item.kind, item.name)
        for item in plan.items
        if item.action in (PlanActionType.CREATE, PlanActionType.UPDATE)
    }
    destroyed_keys = {
        (item.kind, item.name) for item in plan.items if item.action == PlanActionType.DESTROY
    }
    overlapping_keys = active_keys & destroyed_keys

    if overlapping_keys:
        deduped_items: list[PlanItem] = []
        for item in plan.items:
            key = (item.kind, item.name)
            if key in overlapping_keys:
                if item.action == PlanActionType.DESTROY:
                    # Pomijamy DESTROY starego pliku, bo zasób o tym samym UID nadal istnieje w nowym pliku
                    continue
                if item.action == PlanActionType.CREATE:
                    # Skoro ten UID istniał już wcześniej, traktujemy to jako UPDATE istniejącego zasobu
                    item.action = PlanActionType.UPDATE
            deduped_items.append(item)
        plan.items = deduped_items

    action_order = {
        PlanActionType.CREATE: 1,
        PlanActionType.UPDATE: 2,
        PlanActionType.DESTROY: 3,
    }

    def _sort_key(item: PlanItem):
        if item.action == PlanActionType.DESTROY:
            kind_prio = 0 if item.kind != "Folder" else 1
            return (action_order[item.action], -item.depth, kind_prio, str(item.repo_path))
        kind_prio = 0 if item.kind == "Folder" else 1
        return (action_order[item.action], item.depth, kind_prio, str(item.repo_path))

    plan.items.sort(key=_sort_key)
    return plan


def format_terraform_summary(plan: ExecutionPlan, is_apply: bool = False) -> str:
    """
    Generuje zwięzłe i czytelne podsumowanie w stylu Terraform Plan (blok diff)
    zatytułowane `Deploy grafana-observability (TF-style)`.
    """
    lines: list[str] = ["## Deploy grafana-observability (TF-style)", ""]

    if plan.issues:
        lines.append("## Validation Issues ⚠️")
        for issue in plan.issues:
            icon = "❌" if issue.severity == "ERROR" else "⚠️"
            lines.append(f"{icon} {issue.severity} - `{issue.path.name}`: {issue.message}")
        lines.append("")

    if not plan.items:
        prefix = "No changes applied" if is_apply else "No changes"
        lines.append(
            f"{prefix}. Resources are up-to-date (0 to add, 0 to change, 0 to destroy)."
        )
        return "\n".join(lines)

    symbols = {
        PlanActionType.CREATE: "+",
        PlanActionType.UPDATE: "~",
        PlanActionType.DESTROY: "-",
    }

    lines.append("```diff")
    for item in plan.items:
        sym = symbols[item.action]
        location = (
            item.canonical_folder
            if item.kind == "Folder"
            else f"{item.canonical_folder}/{item.title}"
        )
        lines.append(f'{sym} {item.kind:<10} "{location}" ({item.name})')
    lines.append("```")
    lines.append("")
    action_label = "Apply" if is_apply else "Plan"
    lines.append(
        f"## {action_label}: {len(plan.to_add)} to add, {len(plan.to_change)} to change, {len(plan.to_destroy)} to destroy."
    )
    return "\n".join(lines)


def build_gcx_delta_commands(
    plan: ExecutionPlan,
    gcx_bin: str = "./gcx",
    dry_run: bool = False,
) -> list[list[str]]:
    """
    Generuje bezpieczną listę komend dla GCX obejmującą wyłącznie zmienione zasoby (Delta):
    - W trybie `dry_run=True` (plan): używa `gcx resources validate` (zamiast mylącego "resources pushed").
    - W trybie `dry_run=False` (deploy):
      1. Push folderów poziomami od L1 do L4 (rodzic przed dzieckiem).
      2. Push zmienionych/dodanych Dashboardów i AlertRules.
      3. Delete usuniętych Dashboardów i AlertRules.
      4. Delete usuniętych Folderów od L4 w górę do L1 (dziecko przed rodzicem).
    """
    commands: list[list[str]] = []

    push_items = [
        i for i in plan.items if i.action in (PlanActionType.CREATE, PlanActionType.UPDATE)
    ]
    destroy_items = [i for i in plan.items if i.action == PlanActionType.DESTROY]
    kind_to_plural = {
        "Dashboard": "dashboards",
        "AlertRule": "alertrules",
        "RecordingRule": "recordingrules",
        "Folder": "folders",
    }

    if dry_run:
        # W trybie planu: walidacja serwerowa (dry-run) dla dodawanych/zmienianych zasobów
        if push_items:
            cmd = [gcx_bin, "resources", "validate"]
            for item in push_items:
                cmd.extend(["-p", str(item.repo_path)])
            commands.append(cmd)
        return commands

    # Tryb deploy:
    # 0. Pre-flight validate wszystkich wypychanych plików przed jakimkolwiek zapisem do Grafany
    if push_items:
        validate_cmd = [gcx_bin, "resources", "validate"]
        for item in push_items:
            validate_cmd.extend(["-p", str(item.repo_path)])
        commands.append(validate_cmd)

        # 1. Skonsolidowany push wszystkich dodawanych/zmienianych zasobów w jednym poleceniu GCX.
        # Zasoby są uporządkowane topologicznie:
        # najpierw Foldery od poziomu L1 do L4 (rodzic przed dzieckiem),
        # a następnie Dashboardy i AlertRules należące do tych folderów.
        sorted_pushes = sorted(
            push_items,
            key=lambda i: (0 if i.kind == "Folder" else 1, i.depth, str(i.repo_path)),
        )
        push_cmd = [gcx_bin, "resources", "push"]
        for item in sorted_pushes:
            push_cmd.extend(["-p", str(item.repo_path)])
        commands.append(push_cmd)

    # 2. Delete usuniętych Dashboardów i AlertRules
    resource_destroys = [i for i in destroy_items if i.kind != "Folder"]
    if resource_destroys:
        selectors = [f"{kind_to_plural[i.kind]}/{i.name}" for i in resource_destroys]
        cmd = [gcx_bin, "resources", "delete", *selectors, "-y"]
        commands.append(cmd)

    # 4. Delete usuniętych Folderów od najgłębszego do najpłytszego (L4 -> L1)
    folder_destroys = [i for i in destroy_items if i.kind == "Folder"]
    for depth in sorted({i.depth for i in folder_destroys}, reverse=True):
        level_folders = [i for i in folder_destroys if i.depth == depth]
        selectors = [f"folders/{i.name}" for i in level_folders]
        cmd = [gcx_bin, "resources", "delete", *selectors, "-y"]
        commands.append(cmd)

    return commands
