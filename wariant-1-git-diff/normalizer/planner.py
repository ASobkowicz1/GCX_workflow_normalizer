"""Wariant 1: Analiza różnic Git (git diff) + bezpiecznik full-sync (GIT_EMPTY_TREE_HASH)."""

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

GIT_EMPTY_TREE_HASH = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_COMMIT_SHA = "0" * 40


def _run_git(repo_root: Path, args: list[str]) -> str:
    """Uruchamia polecenie git w podanym katalogu repozytorium i zwraca standardowe wyjście."""
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
    data_subdir: str,
) -> str:
    """Wyznacza właściwy commit bazowy do porównania (lub puste drzewo dla 40 zer / full-sync)."""
    if base_ref in (ZERO_COMMIT_SHA, GIT_EMPTY_TREE_HASH):
        return GIT_EMPTY_TREE_HASH

    if target_ref is not None:
        return base_ref

    if base_ref != "HEAD":
        try:
            merge_base = _run_git(repo_root, ["merge-base", base_ref, "HEAD"]).strip()
            if merge_base:
                return merge_base
        except subprocess.CalledProcessError:
            return base_ref
        return base_ref

    wt_diff = _run_git(repo_root, ["diff", "--name-only", "HEAD", "--", data_subdir]).strip()
    untracked = _run_git(
        repo_root, ["ls-files", "--others", "--exclude-standard", "--", data_subdir]
    ).strip()
    if not wt_diff and not untracked:
        try:
            prev_commit = _run_git(repo_root, ["rev-parse", "--verify", "HEAD~1"]).strip()
            if prev_commit:
                return "HEAD~1"
        except subprocess.CalledProcessError:
            return GIT_EMPTY_TREE_HASH
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
    full_sync: bool = False,
) -> ExecutionPlan:
    """
    Buduje plan zmian (Create / Update / Destroy) na podstawie `git diff`.
    Jeśli `full_sync=True`, porównuje cały katalog `data/` z pustym drzewem Git (`GIT_EMPTY_TREE_HASH`),
    dzięki czemu wypycha wszystkie zasoby z repozytorium na wybrany stack Grafany.
    """
    repo_root = repo_root.resolve()
    data_root = (repo_root / data_subdir).resolve()
    plan = ExecutionPlan(issues=list(issues) if issues else [])

    effective_base = (
        GIT_EMPTY_TREE_HASH
        if full_sync
        else _resolve_effective_base_ref(repo_root, base_ref, target_ref, data_subdir)
    )

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
            file_statuses[rel_repo_path] = status[0]  # 'A', 'M', 'D'

    if target_ref is None:
        untracked_out = _run_git(
            repo_root,
            ["ls-files", "--others", "--exclude-standard", "--", data_subdir],
        )
        for line in untracked_out.splitlines():
            rel_repo_path = line.strip()
            if rel_repo_path:
                file_statuses[rel_repo_path] = "A"

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
                    continue
                if item.action == PlanActionType.CREATE:
                    item.action = PlanActionType.UPDATE
            deduped_items.append(item)
        plan.items = deduped_items

    action_order = {
        PlanActionType.CREATE: 1,
        PlanActionType.UPDATE: 2,
        PlanActionType.DESTROY: 3,
    }
    plan.items.sort(key=lambda item: (action_order[item.action], item.depth, str(item.repo_path)))
    return plan


def format_terraform_summary(plan: ExecutionPlan, is_apply: bool = False) -> str:
    """Generuje podsumowanie w stylu Terraform Plan."""
    lines: list[str] = ["## Deploy grafana-observability (TF-style)", ""]

    if plan.issues:
        lines.append("## ⚠️ Validation Issues ⚠️")
        for issue in plan.issues:
            icon = "❌" if issue.severity == "ERROR" else "⚠️"
            lines.append(f"{icon} {issue.severity} - `{issue.path.name}`: {issue.message}")
        lines.append("")

    if not plan.items:
        if is_apply:
            lines.append(
                "No changes applied. Resources are up-to-date (0 added, 0 changed, 0 destroyed)."
            )
        else:
            lines.append(
                "No changes. Resources are up-to-date (0 to add, 0 to change, 0 to destroy)."
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
    if is_apply:
        lines.append(
            f"## Apply: {len(plan.to_add)} added, {len(plan.to_change)} changed, {len(plan.to_destroy)} destroyed."
        )
    else:
        lines.append(
            f"## Plan: {len(plan.to_add)} to add, {len(plan.to_change)} to change, {len(plan.to_destroy)} to destroy."
        )
    return "\n".join(lines)


def build_gcx_delta_commands(
    plan: ExecutionPlan,
    gcx_bin: str = "./gcx",
    dry_run: bool = False,
    context: str | None = None,
) -> list[list[str]]:
    """Generuje listę komend dla GCX obejmującą wyłącznie zmienione zasoby (Delta)."""
    ctx_flags = ["--context", context] if context else []
    commands: list[list[str]] = []

    push_items = [
        i for i in plan.items if i.action in (PlanActionType.CREATE, PlanActionType.UPDATE)
    ]
    destroy_items = [i for i in plan.items if i.action == PlanActionType.DESTROY]
    kind_to_plural = {
        "Dashboard": "dashboards",
        "AlertRule": "alertrules",
        "Folder": "folders",
    }

    if dry_run:
        if push_items:
            cmd = [gcx_bin, "resources", "validate", *ctx_flags]
            for item in push_items:
                cmd.extend(["-p", str(item.repo_path)])
            commands.append(cmd)
        return commands

    if push_items:
        validate_cmd = [gcx_bin, "resources", "validate", *ctx_flags]
        for item in push_items:
            validate_cmd.extend(["-p", str(item.repo_path)])
        commands.append(validate_cmd)

    folder_pushes = [i for i in push_items if i.kind == "Folder"]
    for depth in sorted({i.depth for i in folder_pushes}):
        level_folders = [i for i in folder_pushes if i.depth == depth]
        cmd = [gcx_bin, "resources", "push", *ctx_flags]
        for item in level_folders:
            cmd.extend(["-p", str(item.repo_path)])
        commands.append(cmd)

    resource_pushes = [i for i in push_items if i.kind != "Folder"]
    if resource_pushes:
        cmd = [gcx_bin, "resources", "push", *ctx_flags]
        for item in resource_pushes:
            cmd.extend(["-p", str(item.repo_path)])
        commands.append(cmd)

    resource_destroys = [i for i in destroy_items if i.kind != "Folder"]
    if resource_destroys:
        selectors = [f"{kind_to_plural[i.kind]}/{i.name}" for i in resource_destroys]
        cmd = [gcx_bin, "resources", "delete", *selectors, *ctx_flags, "-y"]
        commands.append(cmd)

    folder_destroys = [i for i in destroy_items if i.kind == "Folder"]
    for depth in sorted({i.depth for i in folder_destroys}, reverse=True):
        level_folders = [i for i in folder_destroys if i.depth == depth]
        selectors = [f"folders/{i.name}" for i in level_folders]
        cmd = [gcx_bin, "resources", "delete", *selectors, *ctx_flags, "-y"]
        commands.append(cmd)

    return commands
