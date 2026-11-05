"""Moduł uzgadniający stan katalogu `data/`: normalizacja plików do `d-*.yaml` / `a-*.yaml` / `r-*.yaml` oraz budowa `folder.yaml`."""

import contextlib
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
    normalize_recordingrule,
)

FOLDER_MANIFEST_NAME = "folder.yaml"
# 1 poziom folderu głównego (L0) + maks. 4 poziomy zagnieżdżeń podfolderów w Grafanie (L1..L4) = 5 poziomów względem katalogu `grafana/`
MAX_GRAFANA_FOLDER_DEPTH = 5


@dataclass
class _CandidateResource:
    """Wewnętrzna reprezentacja pojedynczego zasobu wykrytego w folderze przed zapisem na dysk."""

    source_file: ParsedFile
    title: str
    target_filename: str
    is_raw: bool
    is_alert: bool
    is_recording: bool = False


def _expand_parsed_file_to_candidates(
    parsed: ParsedFile,
    issues: list[ValidationIssue],
) -> list[_CandidateResource]:
    """
    Przekształca wczytany plik w listę zasobów-kandydatów.
    Dla eksportu grupy alertów z UI (apiVersion: 1) wypakowuje każdą regułę jako osobny plik `a-<slug>.yaml` lub `r-<slug>.yaml`.
    Dla dashboardów nadaje docelową nazwę pliku z prefiksem `d-<slug>.yaml`.
    """
    if (
        parsed.kind in (FileKind.RAW_UI_ALERTRULE, FileKind.RAW_UI_RECORDINGRULE)
        and parsed.data
        and str(parsed.data.get("apiVersion", "")) == "1"
    ):
        candidates: list[_CandidateResource] = []
        for group in parsed.data.get("groups", []):
            if not isinstance(group, dict):
                continue
            for rule in group.get("rules", []):
                if not isinstance(rule, dict):
                    continue
                is_rec = "record" in rule or "metric" in rule
                rule_title = rule.get("title")
                if not rule_title or not isinstance(rule_title, str):
                    if is_rec:
                        rec_val = rule.get("record") if "record" in rule else rule.get("metric")
                        if isinstance(rec_val, dict):
                            rule_title = rec_val.get("metric") or rec_val.get("name") or "recording-rule"
                        elif isinstance(rec_val, str) and rec_val:
                            rule_title = rec_val
                        else:
                            rule_title = "recording-rule"
                    else:
                        rule_title = "alert-rule"
                rule_title = str(rule_title).strip()
                if not rule_title:
                    issues.append(
                        ValidationIssue(
                            path=parsed.path,
                            severity=IssueSeverity.ERROR,
                            message="Rule in exported group is missing a title/record name",
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
                    kind=FileKind.RAW_UI_RECORDINGRULE if is_rec else FileKind.RAW_UI_ALERTRULE,
                    data=single_rule_data,
                    title=rule_title,
                )
                prefix = "r" if is_rec else "a"
                try:
                    target_name = slugify_filename(rule_title, ".yaml", prefix=prefix)
                except ValueError as err:
                    issues.append(
                        ValidationIssue(
                            path=parsed.path,
                            severity=IssueSeverity.ERROR,
                            message=f"Rule title {rule_title!r} cannot be converted to filename: {err}",
                        )
                    )
                    continue
                candidates.append(
                    _CandidateResource(
                        source_file=single_parsed,
                        title=rule_title,
                        target_filename=target_name,
                        is_raw=True,
                        is_alert=not is_rec,
                        is_recording=is_rec,
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
    is_recording = parsed.kind in (
        FileKind.RAW_UI_RECORDINGRULE,
        FileKind.CANONICAL_RECORDINGRULE,
    )
    if is_alert:
        file_prefix = "a"
    elif is_recording:
        file_prefix = "r"
    else:
        file_prefix = "d"
    try:
        target_name = slugify_filename(title, ".yaml", prefix=file_prefix)
    except ValueError as err:
        issues.append(
            ValidationIssue(
                path=parsed.path,
                severity=IssueSeverity.ERROR,
                message=f"Resource title {title!r} cannot be converted to filename: {err}",
            )
        )
        return []
    is_raw = parsed.kind in (
        FileKind.RAW_UI_DASHBOARD,
        FileKind.RAW_UI_ALERTRULE,
        FileKind.RAW_UI_RECORDINGRULE,
    )

    return [
        _CandidateResource(
            source_file=parsed,
            title=title,
            target_filename=target_name,
            is_raw=is_raw,
            is_alert=is_alert,
            is_recording=is_recording,
        )
    ]


def _write_yaml_if_changed(target_path: Path, manifest: dict[str, Any], dry_run: bool) -> bool:
    """Zapisuje plik YAML na dysk wyłącznie wtedy, gdy jego treść uległa zmianie (gwarancja idempotentności)."""
    rendered = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)
    if target_path.exists():
        existing = target_path.read_text(encoding="utf-8")
        if existing == rendered:
            return False
    if not dry_run:
        target_path.write_text(rendered, encoding="utf-8")
    return True


def reconcile_data_directory(
    data_root: Path,
    max_depth: int = MAX_GRAFANA_FOLDER_DEPTH,
    dry_run: bool = False,
) -> ReconcileResult:
    """
    Przeprowadza pełną normalizację i walidację katalogu `data/`:
    1. Sprawdza limit zagnieżdżeń folderów Grafany (max 4 poziomy) i ignoruje głębsze katalogi z ostrzeżeniem.
    2. Wykrywa kolizje nazw zasobów w tym samym folderze (odróżniając aktualizację od kolizji).
    3. Zapisuje kanoniczne manifesty `d-<slug>.yaml`, `a-<slug>.yaml` i `r-<slug>.yaml` oraz usuwa surowe pliki eksportów.
    4. Tworzy brakujące `folder.yaml` (idąc od dołu do góry: L4 -> L1) oraz kasuje puste `folder.yaml`.
    """
    data_root = data_root.resolve()
    result = ReconcileResult()
    if not data_root.exists():
        return result

    # KROK 0: Sprawdzenie czy użytkownik nie wrzucił przez pomyłkę dashboardu/alertu bezpośrednio do głównego `data/`
    for root_file in sorted([f for f in data_root.iterdir() if f.is_file()]):
        parsed_root = detect_and_parse(root_file)
        if parsed_root.kind not in (FileKind.IGNORED,):
            result.issues.append(
                ValidationIssue(
                    path=root_file,
                    severity=IssueSeverity.WARNING,
                    message=(
                        f"Resource file '{root_file.name}' is placed directly in root '{data_root.name}/' "
                        "outside any Grafana folder. Move it into a root subfolder."
                    ),
                )
            )

    # Wyliczamy (folder_dir, canonical_path, depth) jednokrotnie dla każdego podkatalogu (bez powtarzania .resolve())
    dir_entries: list[tuple[Path, str, int]] = []
    for p in data_root.rglob("*"):
        if p.is_dir():
            c_path = get_canonical_folder_path(data_root, p)
            if c_path is not None:
                dir_entries.append((p, c_path, get_folder_depth(c_path)))
    dir_entries.sort(key=lambda item: (item[2], item[1]))

    valid_dirs: list[tuple[Path, str, int]] = []
    dirs_with_resources: set[Path] = set()

    # KROK 1: Przetwarzanie zasobów (Dashboard / AlertRule / RecordingRule) w katalogach o głębokości 1..max_depth
    for folder_dir, canonical_path, depth in dir_entries:
        files_in_dir = sorted([f for f in folder_dir.iterdir() if f.is_file()])

        if depth > max_depth:
            user_depth = depth - 1
            user_max_depth = max_depth - 1
            for f in files_in_dir:
                parsed = detect_and_parse(f)
                if parsed.kind not in (FileKind.IGNORED,):
                    result.issues.append(
                        ValidationIssue(
                            path=f,
                            severity=IssueSeverity.WARNING,
                            message=(
                                f"Folder '{canonical_path}' is at depth {user_depth} "
                                f"(max is {user_max_depth}). File is ignored."
                            ),
                        )
                    )
            continue

        valid_dirs.append((folder_dir, canonical_path, depth))
        folder_uid = generate_uid("f", canonical_path)

        raw_files_to_delete: set[Path] = set()
        # Grupowanie zasobów w katalogu po ich docelowej nazwie pliku (np. d-active-users.yaml)
        by_target_name: dict[str, list[_CandidateResource]] = {}
        for f in files_in_dir:
            if f.name == FOLDER_MANIFEST_NAME:
                continue
            parsed = detect_and_parse(f)
            if parsed.kind == FileKind.IGNORED:
                continue
            if parsed.kind == FileKind.INVALID:
                result.issues.append(
                    ValidationIssue(
                        path=f,
                        severity=IssueSeverity.ERROR,
                        message=parsed.error or "Invalid resource file",
                    )
                )
                continue
            if parsed.kind == FileKind.CANONICAL_FOLDER:
                # W folderze może istnieć wyłącznie jeden plik folderu: `folder.yaml`.
                # Jeśli plik ma strukturę manifestu folderu, ale inną nazwę (np. został przemianowany),
                # usuwamy go, aby nie duplikować zasobu.
                raw_files_to_delete.add(f)
                continue

            # Sprawdzenie czy użytkownik nie wyeksportował dashboardu z opcją "Export for sharing externally" (__inputs)
            raw_spec_val = parsed.data.get("spec") if parsed.data else None
            raw_spec: dict[str, Any] = raw_spec_val if isinstance(raw_spec_val, dict) else {}
            if parsed.data and ("__inputs" in parsed.data or "__inputs" in raw_spec):
                result.issues.append(
                    ValidationIssue(
                        path=f,
                        severity=IssueSeverity.ERROR,
                        message=(
                            "Dashboard was exported with 'Export for sharing externally' enabled "
                            "('__inputs' / ${DS_*} placeholders found). Export it without external sharing."
                        ),
                    )
                )
                continue

            for candidate in _expand_parsed_file_to_candidates(parsed, result.issues):
                by_target_name.setdefault(candidate.target_filename, []).append(candidate)

        # Rozstrzyganie: pojedynczy zasób, aktualizacja istniejącego zasobu czy kolizja nazw
        winners: list[_CandidateResource] = []

        for target_filename, group in by_target_name.items():
            if len(group) == 1:
                winner = group[0]
                winners.append(winner)
                if (folder_dir / target_filename) != winner.source_file.path:
                    raw_files_to_delete.add(winner.source_file.path)
            else:
                # Aktualizacja: 1 istniejący plik kanoniczny + 1 nowy surowy eksport o tym samym tytule
                raw_candidates = [c for c in group if c.is_raw]
                canonical_candidates = [c for c in group if not c.is_raw]
                same_type = len({(c.is_alert, c.is_recording) for c in group}) == 1

                if len(raw_candidates) == 1 and len(canonical_candidates) == 1 and same_type:
                    winner = raw_candidates[0]
                    winners.append(winner)
                    for c in group:
                        if (folder_dir / target_filename) != c.source_file.path:
                            raw_files_to_delete.add(c.source_file.path)
                else:
                    sources_str = ", ".join(c.source_file.path.name for c in group)
                    result.issues.append(
                        ValidationIssue(
                            path=folder_dir / target_filename,
                            severity=IssueSeverity.ERROR,
                            message=(
                                f"Name collision for target '{target_filename}' in '{canonical_path}' "
                                f"from files: [{sources_str}]"
                            ),
                        )
                    )

        # Transformacja zwycięskich zasobów do formatu GCX
        normalized_to_write: list[tuple[Path, dict[str, Any], _CandidateResource]] = []

        for winner in winners:
            target_path = folder_dir / winner.target_filename
            if winner.is_alert:
                alert_uid = generate_uid("a", canonical_path, winner.title)
                manifest = normalize_alertrule(winner.source_file, folder_uid, alert_uid)
            elif winner.is_recording:
                rec_uid = generate_uid("r", canonical_path, winner.title)
                manifest = normalize_recordingrule(winner.source_file, folder_uid, rec_uid)
            else:
                dashboard_uid = generate_uid("d", canonical_path, winner.title)
                manifest = normalize_dashboard(winner.source_file, folder_uid, dashboard_uid)

            normalized_to_write.append((target_path, manifest, winner))

        # Zapis kanonicznych plików YAML oraz usunięcie starych surowych plików
        for target_path, manifest, _winner in normalized_to_write:
            if _write_yaml_if_changed(target_path, manifest, dry_run=dry_run):
                result.written_files.append(target_path)
            dirs_with_resources.add(folder_dir)

        for raw_path in sorted(raw_files_to_delete):
            if not result.has_errors:
                if not dry_run and raw_path.exists():
                    raw_path.unlink()
                result.deleted_raw_files.append(raw_path)

    # KROK 2: Zarządzanie manifestami folder.yaml od najgłębszego poziomu w górę (L4 -> L1)
    active_dirs: set[Path] = set(dirs_with_resources)

    for folder_dir, canonical_path, depth in reversed(valid_dirs):
        folder_manifest_path = folder_dir / FOLDER_MANIFEST_NAME
        has_active_child = any(child.parent == folder_dir for child in active_dirs)
        is_active = (folder_dir in dirs_with_resources) or has_active_child

        if is_active:
            active_dirs.add(folder_dir)
            folder_uid = generate_uid("f", canonical_path)
            parent_uid: str | None = None
            if depth > 1:
                parent_canonical = canonical_path.rsplit("/", 1)[0]
                parent_uid = generate_uid("f", parent_canonical)

            folder_manifest = build_folder_manifest(
                title=folder_dir.name,
                folder_uid=folder_uid,
                parent_uid=parent_uid,
            )
            if _write_yaml_if_changed(folder_manifest_path, folder_manifest, dry_run=dry_run):
                result.written_files.append(folder_manifest_path)
        else:
            # Jeśli folder (ani żaden jego podfolder) nie ma już zasobów -> usuwamy sierocy folder.yaml
            if folder_manifest_path.exists():
                if not dry_run:
                    folder_manifest_path.unlink()
                result.deleted_empty_folders.append(folder_manifest_path)
            if not dry_run and folder_dir.exists():
                with contextlib.suppress(OSError):
                    folder_dir.rmdir()

    return result
