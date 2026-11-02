"""Moduł uzgadniający stan katalogu `data/`: normalizacja plików do `d-*.yaml` / `a-*.yaml` oraz budowa `folder.yaml`."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_sync.models import (
    FileKind,
    IssueSeverity,
    ParsedFile,
    ReconcileResult,
    ValidationIssue,
)
from live_sync.parser import detect_and_parse
from live_sync.paths import (
    generate_uid,
    get_canonical_folder_path,
    get_folder_depth,
    slugify_filename,
)
from live_sync.transformer import (
    build_folder_manifest,
    normalize_alertrule,
    normalize_dashboard,
)

FOLDER_MANIFEST_NAME = "folder.yaml"
MAX_GRAFANA_FOLDER_DEPTH = 4


@dataclass
class _CandidateResource:
    """Wewnętrzna reprezentacja pojedynczego zasobu wykrytego w folderze przed zapisem na dysk."""

    source_file: ParsedFile
    title: str
    target_filename: str
    is_raw: bool
    is_alert: bool


def _expand_parsed_file_to_candidates(
    parsed: ParsedFile,
    issues: list[ValidationIssue],
) -> list[_CandidateResource]:
    """Przekształca wczytany plik w listę zasobów-kandydatów."""
    if (
        parsed.kind in (FileKind.RAW_UI_ALERTRULE,)
        and parsed.data
        and parsed.data.get("apiVersion") == 1
    ):
        candidates: list[_CandidateResource] = []
        for group in parsed.data.get("groups", []):
            if not isinstance(group, dict):
                continue
            for rule in group.get("rules", []):
                if not isinstance(rule, dict):
                    continue
                rule_title = str(rule.get("title", "")).strip()
                if not rule_title:
                    issues.append(
                        ValidationIssue(
                            path=parsed.path,
                            severity=IssueSeverity.ERROR,
                            message="Alert rule in exported group is missing a title",
                        )
                    )
                    continue
                single_rule_data = {
                    "apiVersion": 1,
                    "groups": [
                        {
                            "name": group.get("name", "default"),
                            "interval": group.get("interval", "1m"),
                            "rules": [rule],
                        }
                    ],
                }
                single_parsed = ParsedFile(
                    path=parsed.path,
                    kind=FileKind.RAW_UI_ALERTRULE,
                    data=single_rule_data,
                    title=rule_title,
                )
                target_name = slugify_filename(rule_title, ".yaml", prefix="a")
                candidates.append(
                    _CandidateResource(
                        source_file=single_parsed,
                        title=rule_title,
                        target_filename=target_name,
                        is_raw=True,
                        is_alert=True,
                    )
                )
        return candidates

    title = (parsed.title or "").strip()
    if not title:
        issues.append(
            ValidationIssue(
                path=parsed.path,
                severity=IssueSeverity.ERROR,
                message="Resource is missing a title",
            )
        )
        return []

    is_alert = parsed.kind in (FileKind.RAW_UI_ALERTRULE, FileKind.CANONICAL_ALERTRULE)
    file_prefix = "a" if is_alert else "d"
    target_name = slugify_filename(title, ".yaml", prefix=file_prefix)
    is_raw = parsed.kind in (FileKind.RAW_UI_DASHBOARD, FileKind.RAW_UI_ALERTRULE)

    return [
        _CandidateResource(
            source_file=parsed,
            title=title,
            target_filename=target_name,
            is_raw=is_raw,
            is_alert=is_alert,
        )
    ]


def _write_yaml(file_path: Path, data: dict[str, Any]) -> None:
    """Zapisuje słownik do czystego pliku YAML UTF-8 z zachowaniem kolejności kluczy."""
    with file_path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(
            data,
            file,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )


def _reconcile_single_directory(
    dir_path: Path,
    canonical_folder: str,
    folder_uid: str,
    result: ReconcileResult,
) -> None:
    """Normalizuje zasoby wewnątrz pojedynczego podkatalogu."""
    files_in_dir = [p for p in dir_path.iterdir() if p.is_file() and p.name != FOLDER_MANIFEST_NAME]
    candidates: list[_CandidateResource] = []

    for file_path in sorted(files_in_dir):
        parsed = detect_and_parse(file_path)
        if parsed.kind == FileKind.IGNORED:
            continue
        if parsed.kind == FileKind.INVALID:
            result.issues.append(
                ValidationIssue(
                    path=file_path,
                    severity=IssueSeverity.ERROR,
                    message=parsed.error or "Invalid file format",
                )
            )
            continue
        if parsed.kind == FileKind.CANONICAL_FOLDER:
            # W folderze może istnieć wyłącznie jeden plik folderu: `folder.yaml`.
            # Jeśli plik ma strukturę manifestu folderu, ale inną nazwę, usuwamy go.
            if file_path.exists():
                file_path.unlink()
                result.deleted_raw_files.append(file_path)
            continue

        expanded = _expand_parsed_file_to_candidates(parsed, result.issues)
        candidates.extend(expanded)

    title_to_candidate: dict[str, _CandidateResource] = {}
    for cand in candidates:
        norm_title = cand.title.strip().lower()
        if norm_title in title_to_candidate:
            prev = title_to_candidate[norm_title]
            result.issues.append(
                ValidationIssue(
                    path=cand.source_file.path,
                    severity=IssueSeverity.ERROR,
                    message=(
                        f"Duplicate resource title '{cand.title}' in folder '{canonical_folder}'. "
                        f"Conflicts with: {prev.source_file.path.name}"
                    ),
                )
            )
            continue
        title_to_candidate[norm_title] = cand

    for cand in title_to_candidate.values():
        target_path = dir_path / cand.target_filename
        prefix = "a" if cand.is_alert else "d"
        resource_uid = generate_uid(prefix, canonical_folder, cand.title)

        if cand.is_alert:
            manifest = normalize_alertrule(cand.source_file, folder_uid, resource_uid)
        else:
            manifest = normalize_dashboard(cand.source_file, folder_uid, resource_uid)

        _write_yaml(target_path, manifest)
        result.written_files.append(target_path)

        src_path = cand.source_file.path
        if src_path.resolve() != target_path.resolve() and src_path.exists():
            src_path.unlink()
            result.deleted_raw_files.append(src_path)


def reconcile_data_directory(data_root: Path, dry_run: bool = False) -> ReconcileResult:
    """
    Skanuje cały katalog `data/`, buduje hierarchię folderów L1-L4 w plikach `folder.yaml`
    oraz normalizuje dashboardy i reguły alertów do formatu `d-*.yaml` i `a-*.yaml`.
    """
    result = ReconcileResult()
    data_root = data_root.resolve()
    if not data_root.exists() or not data_root.is_dir():
        return result

    for root_file in sorted(data_root.iterdir()):
        if not root_file.is_file():
            continue
        parsed = detect_and_parse(root_file)
        if parsed.kind in (
            FileKind.CANONICAL_DASHBOARD,
            FileKind.CANONICAL_ALERTRULE,
            FileKind.RAW_UI_DASHBOARD,
            FileKind.RAW_UI_ALERTRULE,
        ):
            result.issues.append(
                ValidationIssue(
                    path=root_file,
                    severity=IssueSeverity.ERROR,
                    message="Dashboards and Alert Rules cannot be placed directly in root data/ directory. Place them in a folder.",
                )
            )

    all_dirs = [d for d in data_root.rglob("*") if d.is_dir()]
    all_dirs.sort(key=lambda d: len(d.parts))

    folder_uids: dict[str, str] = {}

    for dir_path in all_dirs:
        canonical_folder = get_canonical_folder_path(data_root, dir_path)
        if canonical_folder is None:
            continue

        depth = get_folder_depth(canonical_folder)
        if depth > MAX_GRAFANA_FOLDER_DEPTH:
            result.issues.append(
                ValidationIssue(
                    path=dir_path,
                    severity=IssueSeverity.ERROR,
                    message=(
                        f"Folder depth ({depth}) exceeds Grafana maximum limit of "
                        f"{MAX_GRAFANA_FOLDER_DEPTH} levels: '{canonical_folder}'"
                    ),
                )
            )
            continue

        folder_title = dir_path.name
        folder_uid = generate_uid("f", canonical_folder)
        folder_uids[canonical_folder] = folder_uid

        parent_canonical = (
            canonical_folder.rsplit("/", 1)[0] if "/" in canonical_folder else None
        )
        parent_uid = folder_uids.get(parent_canonical) if parent_canonical else None

        folder_manifest_path = dir_path / FOLDER_MANIFEST_NAME
        folder_manifest = build_folder_manifest(
            title=folder_title, folder_uid=folder_uid, parent_uid=parent_uid
        )
        _write_yaml(folder_manifest_path, folder_manifest)
        result.written_files.append(folder_manifest_path)

        _reconcile_single_directory(dir_path, canonical_folder, folder_uid, result)

    _clean_empty_folders(data_root, result)
    return result


def _clean_empty_folders(data_root: Path, result: ReconcileResult) -> None:
    """Usuwa puste foldery oraz foldery zawierające wyłącznie osierocony `folder.yaml`."""
    all_dirs = [d for d in data_root.rglob("*") if d.is_dir()]
    all_dirs.sort(key=lambda d: len(d.parts), reverse=True)

    for dir_path in all_dirs:
        entries = list(dir_path.iterdir())
        is_empty = len(entries) == 0
        is_only_folder_manifest = len(entries) == 1 and entries[0].name == FOLDER_MANIFEST_NAME

        if is_only_folder_manifest:
            entries[0].unlink()
            entries = []
            is_empty = True

        if is_empty:
            dir_path.rmdir()
            result.deleted_empty_folders.append(dir_path)
