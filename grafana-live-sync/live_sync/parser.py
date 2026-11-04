"""Detekcja typu pliku oraz parsowanie zawartości JSON/YAML dla eksportów z UI Grafany i manifestów GCX."""

import json
from pathlib import Path
from typing import Any

import yaml

from live_sync.models import FileKind, ParsedFile
from live_sync.paths import is_managed_uid

IGNORED_EXTENSIONS = {".md", ".txt", ".prom", ".tf", ".sh", ".gitignore", ".gitkeep"}
IGNORED_FILENAMES = {".DS_Store", "README.md", ".gitkeep", ".gitignore"}


def detect_and_parse(file_path: Path) -> ParsedFile:
    """
    Wczytuje plik z dysku i rozpoznaje jego typ:
    - ignoruje pliki niebędące JSON/YAML (np. readme.md),
    - odróżnia surowe eksporty z Grafana UI od czystych, kanonicznych manifestów GCX,
    - wyciąga tytuł zasobu (title) potrzebny do wygenerowania nazwy pliku i UID.
    """
    if file_path.name in IGNORED_FILENAMES or file_path.name.startswith("."):
        return ParsedFile(path=file_path, kind=FileKind.IGNORED)

    file_ext = file_path.suffix.lower()
    if file_ext in IGNORED_EXTENSIONS or file_ext not in (".json", ".yaml", ".yml"):
        return ParsedFile(path=file_path, kind=FileKind.IGNORED)

    try:
        with file_path.open(encoding="utf-8") as file:
            data = json.load(file) if file_ext == ".json" else yaml.safe_load(file)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        return ParsedFile(path=file_path, kind=FileKind.INVALID, error=str(exc))

    if not isinstance(data, dict):
        return ParsedFile(
            path=file_path,
            kind=FileKind.INVALID,
            error="file content must be a JSON/YAML object (dictionary)",
        )

    api_version = str(data.get("apiVersion", ""))
    kind = str(data.get("kind", ""))
    raw_metadata = data.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    meta_name = str(metadata.get("name", ""))
    raw_spec = data.get("spec")
    spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    # Pola dopisywane przez serwer Grafany przy eksporcie — ich obecność oznacza surowy eksport z UI
    has_server_fields = (
        any(
            key in metadata
            for key in (
                "uid",
                "resourceVersion",
                "namespace",
                "creationTimestamp",
                "generation",
                "managedFields",
            )
        )
        or "status" in data
    )

    def is_canonical_uid(prefix: str) -> bool:
        return (
            file_ext in (".yaml", ".yml")
            and meta_name.startswith(f"{prefix}-")
            and is_managed_uid(meta_name)
            and not has_server_fields
        )

    # 1. Rozpoznanie Dashboardu (format k8s v2/v1 lub klasyczny format v1 JSON z tablicą panels)
    if api_version.startswith("dashboard.grafana.app/") and kind == "Dashboard":
        title = spec.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            return ParsedFile(
                path=file_path,
                kind=FileKind.INVALID,
                data=data,
                error="Dashboard is missing a valid spec.title",
            )
        dashboard_kind = (
            FileKind.CANONICAL_DASHBOARD if is_canonical_uid("d") else FileKind.RAW_UI_DASHBOARD
        )
        return ParsedFile(path=file_path, kind=dashboard_kind, data=data, title=title.strip())

    if "panels" in data and "schemaVersion" in data:
        title = data.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            return ParsedFile(
                path=file_path,
                kind=FileKind.INVALID,
                data=data,
                error="Classic JSON Dashboard is missing a valid title",
            )
        return ParsedFile(
            path=file_path, kind=FileKind.RAW_UI_DASHBOARD, data=data, title=title.strip()
        )

    # 2. Rozpoznanie Folderu (manifest folder.yaml)
    if api_version.startswith("folder.grafana.app/") and kind == "Folder":
        title = spec.get("title")
        return ParsedFile(path=file_path, kind=FileKind.CANONICAL_FOLDER, data=data, title=title)

    # 3. Rozpoznanie AlertRule (format k8s v0alpha1 / v1beta1)
    if api_version.startswith("rules.alerting.grafana.app/") and kind == "AlertRule":
        title = spec.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            return ParsedFile(
                path=file_path,
                kind=FileKind.INVALID,
                data=data,
                error="AlertRule is missing a valid spec.title",
            )
        alert_kind = (
            FileKind.CANONICAL_ALERTRULE if is_canonical_uid("a") else FileKind.RAW_UI_ALERTRULE
        )
        return ParsedFile(path=file_path, kind=alert_kind, data=data, title=title.strip())

    # 4. Rozpoznanie RecordingRule (format k8s v0alpha1 / v1beta1)
    if api_version.startswith("rules.alerting.grafana.app/") and kind == "RecordingRule":
        metric_val = spec.get("metric")
        title = spec.get("title")
        if not title or not isinstance(title, str):
            if isinstance(metric_val, str):
                title = metric_val
            elif isinstance(metric_val, dict):
                title = metric_val.get("metric") or metric_val.get("name") or "recording-rule"
            else:
                title = "recording-rule"

        is_canonical = (
            is_canonical_uid("r")
            and isinstance(metric_val, str)
            and bool(metric_val.strip())
            and bool(spec.get("targetDatasourceUID"))
        )
        rec_kind = (
            FileKind.CANONICAL_RECORDINGRULE
            if is_canonical
            else FileKind.RAW_UI_RECORDINGRULE
        )
        return ParsedFile(path=file_path, kind=rec_kind, data=data, title=str(title).strip())

    if api_version == "1" and isinstance(data.get("groups"), list):
        first_rule: dict[str, Any] | None = None
        for grp in data["groups"]:
            if isinstance(grp, dict) and isinstance(grp.get("rules"), list):
                for r in grp["rules"]:
                    if isinstance(r, dict):
                        first_rule = r
                        break
            if first_rule is not None:
                break

        if first_rule is not None:
            is_rec = "record" in first_rule or "metric" in first_rule
            title = first_rule.get("title")
            if not title or not isinstance(title, str):
                if is_rec:
                    rec_val = (
                        first_rule.get("record")
                        if "record" in first_rule
                        else first_rule.get("metric")
                    )
                    if isinstance(rec_val, dict):
                        title = rec_val.get("metric") or rec_val.get("name") or "recording-rule"
                    else:
                        title = rec_val or "recording-rule"
                else:
                    title = "alert-rule"
            rec_kind = (
                FileKind.RAW_UI_RECORDINGRULE if is_rec else FileKind.RAW_UI_ALERTRULE
            )
            return ParsedFile(
                path=file_path,
                kind=rec_kind,
                data=data,
                title=str(title).strip() if title else None,
            )

    return ParsedFile(
        path=file_path,
        kind=FileKind.INVALID,
        data=data,
        error="unrecognized Grafana resource structure",
    )
