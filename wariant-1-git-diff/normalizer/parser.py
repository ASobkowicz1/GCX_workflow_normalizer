"""Detekcja typu pliku oraz parsowanie zawartości JSON/YAML dla eksportów z UI Grafany i manifestów GCX."""

import json
from pathlib import Path
from typing import Any

import yaml

from normalizer.models import FileKind, ParsedFile


def detect_and_parse(file_path: Path) -> ParsedFile:
    """
    Wczytuje plik z dysku i rozpoznaje jego typ:
    - ignoruje pliki niebędące JSON/YAML (np. readme.md),
    - odróżnia surowe eksporty z Grafana UI od czystych, kanonicznych manifestów GCX,
    - wyciąga tytuł zasobu (title) potrzebny do wygenerowania nazwy pliku i UID.
    """
    file_ext = file_path.suffix.lower()
    if file_ext not in (".json", ".yaml", ".yml"):
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
    # Pola dopisywane przez serwer Grafany przy eksporcie — ich obecność oznacza surowy eksport z UI
    has_server_fields = any(
        key in metadata for key in ("uid", "resourceVersion", "namespace", "creationTimestamp")
    )

    def is_canonical_uid(prefix: str) -> bool:
        return (
            file_ext in (".yaml", ".yml")
            and len(meta_name) == 22
            and meta_name.startswith(f"{prefix}-")
            and not has_server_fields
        )

    # 1. Rozpoznanie Dashboardu (format k8s v2/v1beta1 lub klasyczny format v1 JSON z tablicą panels)
    if api_version.startswith("dashboard.grafana.app/") and kind == "Dashboard":
        title = data.get("spec", {}).get("title")
        dashboard_kind = (
            FileKind.CANONICAL_DASHBOARD if is_canonical_uid("d") else FileKind.RAW_UI_DASHBOARD
        )
        return ParsedFile(path=file_path, kind=dashboard_kind, data=data, title=title)

    if "panels" in data and "schemaVersion" in data:
        return ParsedFile(
            path=file_path, kind=FileKind.RAW_UI_DASHBOARD, data=data, title=data.get("title")
        )

    # 2. Rozpoznanie Folderu (manifest folder.yaml)
    if api_version.startswith("folder.grafana.app/") and kind == "Folder":
        title = data.get("spec", {}).get("title")
        return ParsedFile(path=file_path, kind=FileKind.CANONICAL_FOLDER, data=data, title=title)

    # 3. Rozpoznanie AlertRule (format k8s v0alpha1 lub eksport grupy alertów z UI z apiVersion: 1)
    if api_version.startswith("rules.alerting.grafana.app/") and kind == "AlertRule":
        title = data.get("spec", {}).get("title")
        alert_kind = (
            FileKind.CANONICAL_ALERTRULE if is_canonical_uid("a") else FileKind.RAW_UI_ALERTRULE
        )
        return ParsedFile(path=file_path, kind=alert_kind, data=data, title=title)

    if api_version == "1" and isinstance(data.get("groups"), list):
        groups = data["groups"]
        rules = groups[0].get("rules", []) if groups and isinstance(groups[0], dict) else []
        title = rules[0].get("title") if rules and isinstance(rules[0], dict) else None
        return ParsedFile(path=file_path, kind=FileKind.RAW_UI_ALERTRULE, data=data, title=title)

    return ParsedFile(
        path=file_path,
        kind=FileKind.INVALID,
        data=data,
        error="unrecognized Grafana resource structure",
    )
