"""Budowanie planu w stylu Terraform oraz generowanie komend GCX na podstawie porównania repozytorium ze stanem Grafany."""

from pathlib import Path
from typing import Any

from live_sync.grafana_state import LiveGrafanaResource, canonicalize_spec
from live_sync.models import (
    ExecutionPlan,
    FileKind,
    PlanActionType,
    PlanItem,
    ValidationIssue,
)
from live_sync.parser import detect_and_parse
from live_sync.paths import get_canonical_folder_path, get_folder_depth
from live_sync.reconciler import MAX_GRAFANA_FOLDER_DEPTH


def _extract_local_resource(
    data: dict[str, Any], fallback_path: Path
) -> tuple[str, str, str, str | None, dict[str, Any]]:
    """Wyciąga (kind, name, title, folder_uid, canonical_spec) z kanonicznego pliku YAML z `data/`."""
    kind = str(data.get("kind", "Unknown"))
    raw_meta = data.get("metadata")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    name = str(meta.get("name", fallback_path.stem))
    raw_annotations = meta.get("annotations")
    annotations: dict[str, Any] = raw_annotations if isinstance(raw_annotations, dict) else {}
    folder_uid = annotations.get("grafana.app/folder")
    raw_spec = data.get("spec")
    spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    title = str(spec.get("title", fallback_path.stem))
    return kind, name, title, folder_uid, canonicalize_spec(kind, spec)


def _resolve_live_folder_path_and_depth(
    folder_uid: str | None,
    live_resources: dict[tuple[str, str], LiveGrafanaResource],
    local_folder_by_uid: dict[str, str],
) -> tuple[str, int]:
    """
    Odtwarza ścieżkę kanoniczną i głębokość dla zasobu znajdującego się w Grafanie (do poprawnego kolejkowania DESTROY L4 -> L1).
    """
    if not folder_uid:
        return "General", 1

    if folder_uid in local_folder_by_uid:
        c_path = local_folder_by_uid[folder_uid]
        return c_path, get_folder_depth(c_path)

    # Przechodzimy w górę po rodzicach folderów w live_resources
    titles: list[str] = []
    curr_uid: str | None = folder_uid
    visited: set[str] = set()
    while curr_uid and curr_uid not in visited:
        visited.add(curr_uid)
        if curr_uid in local_folder_by_uid:
            prefix_path = local_folder_by_uid[curr_uid]
            full_path = "/".join([prefix_path, *reversed(titles)]) if titles else prefix_path
            return full_path, get_folder_depth(full_path)
        folder_res = live_resources.get(("Folder", curr_uid))
        if folder_res is None:
            break
        titles.append(folder_res.title)
        curr_uid = folder_res.folder_uid

    if titles:
        reconstructed = "/".join(reversed(titles))
        return reconstructed, get_folder_depth(reconstructed)
    return folder_uid, 1


def build_live_state_plan(
    repo_root: Path,
    live_resources: dict[tuple[str, str], LiveGrafanaResource],
    data_subdir: str = "grafana",
    issues: list[ValidationIssue] | None = None,
) -> ExecutionPlan:
    """
    Buduje plan zmian (Create / Update / Destroy) tak jak Terraform lub ArgoCD — poprzez bezpośrednie
    porównanie plików w katalogu `data/` ze stanem zwróconym przez API Grafany (`live_resources`):
    1. Zasób jest w `data/`, a nie ma go w Grafanie -> `+ CREATE` (również gdy ktoś skasował go ręcznie w UI).
    2. Zasób jest w `data/` i w Grafanie, ale różni się `spec` lub folder nadrzędny -> `~ UPDATE` (wykrywa też drift w UI).
    3. Zasób jest w `data/` i w Grafanie, a `spec` jest identyczny -> pomijamy (brak zmian).
    4. Zasobu nie ma w `data/`, a jest w Grafanie (i ma nasz deterministyczny UID `f-/d-/a-/r-`) -> `- DESTROY`.
    """
    repo_root = repo_root.resolve()
    data_root = (repo_root / data_subdir).resolve()
    plan = ExecutionPlan(issues=list(issues) if issues else [])

    desired_keys: set[tuple[str, str]] = set()
    local_folder_by_uid: dict[str, str] = {}

    if data_root.exists():
        for abs_path in sorted(data_root.rglob("*")):
            if not abs_path.is_file():
                continue
            canonical_folder = get_canonical_folder_path(data_root, abs_path)
            if canonical_folder is None:
                continue
            depth = get_folder_depth(canonical_folder)
            if depth > MAX_GRAFANA_FOLDER_DEPTH:
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

            kind, name, title, folder_uid, local_spec = _extract_local_resource(parsed.data, abs_path)
            key = (kind, name)
            desired_keys.add(key)
            if kind == "Folder":
                local_folder_by_uid[name] = canonical_folder

            rel_repo_path = abs_path.relative_to(repo_root)
            live_res = live_resources.get(key)

            if live_res is None:
                # Zasób istnieje w Git (data/), ale nie istnieje jeszcze w Grafanie -> CREATE
                plan.items.append(
                    PlanItem(
                        action=PlanActionType.CREATE,
                        kind=kind,
                        name=name,
                        title=title,
                        canonical_folder=canonical_folder,
                        repo_path=rel_repo_path,
                        depth=depth,
                    )
                )
            else:
                # Zasób istnieje w obu miejscach -> sprawdzamy czy jego specyfikacja lub folder uległy zmianie
                if live_res.folder_uid != folder_uid or live_res.spec != local_spec:
                    plan.items.append(
                        PlanItem(
                            action=PlanActionType.UPDATE,
                            kind=kind,
                            name=name,
                            title=title,
                            canonical_folder=canonical_folder,
                            repo_path=rel_repo_path,
                            depth=depth,
                        )
                    )

    # Sprawdzenie zasobów, które są w Grafanie (z naszym deterministycznym UID), ale zniknęły z katalogu `data/` -> DESTROY
    for key, live_res in sorted(live_resources.items()):
        if key in desired_keys:
            continue

        if live_res.kind == "Folder":
            parent_path, parent_depth = _resolve_live_folder_path_and_depth(
                live_res.folder_uid, live_resources, local_folder_by_uid
            )
            if live_res.folder_uid is None:
                canonical_folder = live_res.title
                depth = 1
            else:
                canonical_folder = f"{parent_path}/{live_res.title}"
                depth = parent_depth + 1
            repo_path = Path(data_subdir) / canonical_folder / "folder.yaml"
        else:
            canonical_folder, depth = _resolve_live_folder_path_and_depth(
                live_res.folder_uid, live_resources, local_folder_by_uid
            )
            repo_path = Path(data_subdir) / canonical_folder / f"{live_res.name}.yaml"

        plan.items.append(
            PlanItem(
                action=PlanActionType.DESTROY,
                kind=live_res.kind,
                name=live_res.name,
                title=live_res.title,
                canonical_folder=canonical_folder,
                repo_path=repo_path,
                depth=depth,
            )
        )

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
    """Generuje podsumowanie w stylu Terraform Plan."""
    lines: list[str] = ["## Deploy grafana-observability (terraform-style)", ""]

    if plan.issues:
        lines.append("## Validation Issues ⚠️")
        for issue in plan.issues:
            icon = "❌" if issue.severity == "ERROR" else "⚠️"
            lines.append(f"{icon} {issue.severity} - `{issue.path.name}`: {issue.message}")
        lines.append("")

    if not plan.items:
        prefix = "## No changes applied" if is_apply else "## No changes"
        lines.append(f"{prefix}. Grafana state matches repository (0 to add, 0 to change, 0 to destroy).")
        return "\n".join(lines)

    symbols = {
        PlanActionType.CREATE: "+",
        PlanActionType.UPDATE: "~",
        PlanActionType.DESTROY: "-",
    }

    lines.append("```diff")
    for item in plan.items:
        sym = symbols[item.action]
        location = item.canonical_folder if item.kind == "Folder" else f"{item.canonical_folder}/{item.title}"
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
    """Generuje bezpieczną listę komend dla GCX obejmującą wyłącznie zasoby różniące się od stanu Grafany."""
    commands: list[list[str]] = []

    push_items = [i for i in plan.items if i.action in (PlanActionType.CREATE, PlanActionType.UPDATE)]
    destroy_items = [i for i in plan.items if i.action == PlanActionType.DESTROY]
    kind_to_plural = {
        "Dashboard": "dashboards",
        "AlertRule": "alertrules",
        "RecordingRule": "recordingrules",
        "Folder": "folders",
    }

    if dry_run:
        if push_items:
            cmd = [gcx_bin, "resources", "validate"]
            for item in push_items:
                cmd.extend(["-p", str(item.repo_path)])
            commands.append(cmd)
        return commands

    if push_items:
        validate_cmd = [gcx_bin, "resources", "validate"]
        for item in push_items:
            validate_cmd.extend(["-p", str(item.repo_path)])
        commands.append(validate_cmd)

        sorted_pushes = sorted(
            push_items,
            key=lambda i: (0 if i.kind == "Folder" else 1, i.depth, str(i.repo_path)),
        )
        push_cmd = [gcx_bin, "resources", "push"]
        for item in sorted_pushes:
            push_cmd.extend(["-p", str(item.repo_path)])
        commands.append(push_cmd)

    resource_destroys = [i for i in destroy_items if i.kind != "Folder"]
    if resource_destroys:
        selectors = [f"{kind_to_plural.get(i.kind, f'{i.kind.lower()}s')}/{i.name}" for i in resource_destroys]
        cmd = [gcx_bin, "resources", "delete", *selectors, "-y"]
        commands.append(cmd)

    folder_destroys = [i for i in destroy_items if i.kind == "Folder"]
    for depth in sorted({i.depth for i in folder_destroys}, reverse=True):
        level_folders = [i for i in folder_destroys if i.depth == depth]
        selectors = [f"folders/{i.name}" for i in level_folders]
        cmd = [gcx_bin, "resources", "delete", *selectors, "-y"]
        commands.append(cmd)

    return commands
