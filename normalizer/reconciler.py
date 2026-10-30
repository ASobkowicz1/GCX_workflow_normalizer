"""Moduł uzgadniający stan katalogu `data/`: normalizacja plików do `d-*.yaml` / `a-*.yaml` oraz budowa `folder.yaml`."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from normalizer.models import (
    FileKind,
    IssueSeverity,
    ParsedFile,
    ReconcileResult,
    ValidationIssue,
)
from normalizer.parser import detect_and_parse
from normalizer.paths import (
    generate_uid,
    get_canonical_folder_path,
    get_folder_depth,
    slugify_filename,
)
from normalizer.transformer import (
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
    """
    Przekształca wczytany plik w listę zasobów-kandydatów.
    Dla eksportu grupy alertów z UI (apiVersion: 1) wypakowuje każdą regułę jako osobny plik `a-<slug>.yaml`.
    Dla dashboardów nadaje docelową nazwę pliku z prefiksem `d-<slug>.yaml`.
    """
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
    3. Zapisuje kanoniczne manifesty `d-<slug>.yaml` i `a-<slug>.yaml` oraz usuwa surowe pliki eksportów.
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

    # KROK 1: Przetwarzanie zasobów (Dashboard / AlertRule) w katalogach o głębokości 1..max_depth
    for folder_dir, canonical_path, depth in dir_entries:
        files_in_dir = sorted([f for f in folder_dir.iterdir() if f.is_file()])

        if depth > max_depth:
            for f in files_in_dir:
                parsed = detect_and_parse(f)
                if parsed.kind not in (FileKind.IGNORED,):
                    result.issues.append(
                        ValidationIssue(
                            path=f,
                            severity=IssueSeverity.WARNING,
                            message=(
                                f"Folder '{canonical_path}' is at depth {depth} "
                                f"(max allowed is {max_depth}). File is ignored."
                            ),
                        )
                    )
            continue

        valid_dirs.append((folder_dir, canonical_path, depth))
        folder_uid = generate_uid("f", canonical_path)

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
                continue

            # Sprawdzenie czy użytkownik nie wyeksportował dashboardu z opcją "Export for sharing externally" (__inputs)
            if parsed.data and "__inputs" in parsed.data:
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
        raw_files_to_delete: set[Path] = set()

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
                same_type = len({c.is_alert for c in group}) == 1

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

    return result
