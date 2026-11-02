"""Detekcja typu pliku oraz parsowanie zawartości JSON/YAML dla eksportów z UI Grafany i manifestów GCX."""

import json
from pathlib import Path
from typing import Any

import yaml

from live_sync.models import FileKind, ParsedFile


def detect_and_parse(file_path: Path) -> ParsedFile:
    """Wczytuje plik z dysku i rozpoznaje jego typ."""
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

    if api_version.startswith("folder.grafana.app/") and kind == "Folder":
        title = data.get("spec", {}).get("title")
        return ParsedFile(path=file_path, kind=FileKind.CANONICAL_FOLDER, data=data, title=title)

    if api_version.startswith("rules.alerting.grafana.app/") and kind == "AlertRule":
        title = data.get("spec", {}).get("title")
        alert_kind = (
            FileKind.CANONICAL_ALERTRULE if is_canonical_uid("a") else FileKind.RAW_UI_ALERTRULE
        )
        return ParsedFile(path=file_path, kind=alert_kind, data=data, title=title)

    # 4. Rozpoznanie RecordingRule (format k8s v0alpha1 / v1beta1)
    if api_version.startswith("rules.alerting.grafana.app/") and kind == "RecordingRule":
        spec = data.get("spec") if isinstance(data.get("spec"), dict) else {}
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
        return ParsedFile(path=file_path, kind=rec_kind, data=data, title=title)

    if api_version == "1" and isinstance(data.get("groups"), list):
        groups = data["groups"]
        rules = groups[0].get("rules", []) if groups and isinstance(groups[0], dict) else []
        if rules and isinstance(rules[0], dict):
            first_rule = rules[0]
            if "record" in first_rule or "metric" in first_rule:
                rec_val = first_rule.get("record") or first_rule.get("metric")
                if isinstance(rec_val, dict):
                    title = rec_val.get("metric") or rec_val.get("name") or first_rule.get("title")
                else:
                    title = rec_val or first_rule.get("title")
                return ParsedFile(
                    path=file_path,
                    kind=FileKind.RAW_UI_RECORDINGRULE,
                    data=data,
                    title=str(title) if title else None,
                )
            title = first_rule.get("title")
            return ParsedFile(path=file_path, kind=FileKind.RAW_UI_ALERTRULE, data=data, title=title)

    return ParsedFile(
        path=file_path,
        kind=FileKind.INVALID,
        data=data,
        error="unrecognized Grafana resource structure",
    )
