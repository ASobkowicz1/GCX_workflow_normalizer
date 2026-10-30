"""Pobieranie aktualnego stanu zasobów z żywej Grafany za pomocą `gcx resources pull` (obsługuje OAuth Keychain oraz Tokeny SA)."""

import copy
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANAGED_UID_PATTERN = re.compile(r"^(f|d|a)-[0-9a-f]{20}$")
DURATION_PART_RE = re.compile(r"(\d+)(ns|us|µs|ms|s|m|h|d|w)")


@dataclass
class LiveGrafanaResource:
    """Zasób pobrany bezpośrednio z żywego API Grafany."""

    kind: str  # "Folder", "Dashboard", "AlertRule"
    name: str  # metadata.name (UID zasobu, np. d-...)
    title: str
    folder_uid: str | None
    spec: dict[str, Any]


def is_managed_uid(name: str) -> bool:
    """
    Sprawdza, czy UID zasobu pasuje do naszego deterministycznego formatu `^(f|d|a)-[0-9a-f]{20}$`.
    Zasoby stworzone ręcznie w UI Grafany mają inny format UID, więc są w 100% chronione przed usunięciem.
    """
    return bool(MANAGED_UID_PATTERN.match(name))


def resolve_gcx_bin(gcx_bin: str) -> str:
    """Rozwiązuje ścieżkę do binarki gcx (sprawdza ./gcx oraz zmienną PATH)."""
    if Path(gcx_bin).exists():
        return str(Path(gcx_bin).resolve())
    found = shutil.which(gcx_bin) or shutil.which("gcx")
    return found or gcx_bin


def _normalize_duration(val: Any) -> str:
    """Normalizuje czas trwania (np. '1m', '1m0s', '60s', '10m0s', '600s') do kanonicznej liczby sekund."""
    if val is None:
        return "0s"
    if isinstance(val, (int, float)):
        return f"{int(val)}s"
    s = str(val).strip()
    if not s or s in ("0", "0s", "0m", "0h"):
        return "0s"
    if s.isdigit():
        return f"{int(s)}s"

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }
    total_seconds = 0
    matched = False
    for amount_str, unit in DURATION_PART_RE.findall(s):
        if unit in multipliers:
            total_seconds += int(amount_str) * multipliers[unit]
            matched = True
    if matched:
        return f"{total_seconds}s"
    return s


def _strip_empty_and_defaults(obj: Any) -> Any:
    """Rekurencyjnie usuwa puste kolekcje, wartości False oraz domyślne pola Go (`omitempty`)."""
    if isinstance(obj, dict):
        cleaned_dict: dict[str, Any] = {}
        for k, v in obj.items():
            if k in ("hide", "intervalMs", "maxDataPoints"):
                continue
            if v is False:
                continue
            cleaned_v = _strip_empty_and_defaults(v)
            if cleaned_v in (None, "", [], {}) or cleaned_v is False:
                continue
            cleaned_dict[k] = cleaned_v
        return cleaned_dict
    if isinstance(obj, list):
        cleaned_list = [_strip_empty_and_defaults(item) for item in obj]
        return [item for item in cleaned_list if item not in (None, "", [], {}) and item is not False]
    return obj


def canonicalize_spec(kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Oczyszcza słownik `spec` z pól dynamicznie dopisywanych lub pomijanych przez serwer Grafany."""
    cleaned = copy.deepcopy(spec)
    for field in (
        "id",
        "uid",
        "version",
        "gnetId",
        "folderUid",
        "__inputs",
        "__elements",
        "__requires",
    ):
        cleaned.pop(field, None)

    if kind == "Folder":
        return {
            "title": str(cleaned.get("title", "")).strip(),
        }

    if kind == "AlertRule":
        raw_exprs = cleaned.get("expressions")
        norm_exprs: dict[str, Any] = {}
        if isinstance(raw_exprs, dict):
            for ref_id, expr_obj in sorted(raw_exprs.items()):
                if not isinstance(expr_obj, dict):
                    continue
                model = _strip_empty_and_defaults(expr_obj.get("model", {}))
                entry: dict[str, Any] = {
                    "model": model,
                    "source": bool(expr_obj.get("source", False)),
                }
                ds_uid = expr_obj.get("datasourceUID")
                if ds_uid and ds_uid != "__expr__":
                    entry["datasourceUID"] = str(ds_uid)
                q_type = expr_obj.get("queryType")
                if q_type:
                    entry["queryType"] = str(q_type)
                rtr = expr_obj.get("relativeTimeRange")
                if isinstance(rtr, dict):
                    rtr_from = _normalize_duration(rtr.get("from", 0))
                    rtr_to = _normalize_duration(rtr.get("to", 0))
                    if not (rtr_from == "0s" and rtr_to == "0s"):
                        entry["relativeTimeRange"] = {
                            "from": rtr_from,
                            "to": rtr_to,
                        }
                norm_exprs[str(ref_id)] = entry

        trigger = cleaned.get("trigger") if isinstance(cleaned.get("trigger"), dict) else {}
        notif = (
            cleaned.get("notificationSettings")
            if isinstance(cleaned.get("notificationSettings"), dict)
            else {}
        )
        norm_notif = _strip_empty_and_defaults(notif)
        if isinstance(norm_notif, dict):
            norm_notif.pop("type", None)

        return {
            "title": str(cleaned.get("title", "")).strip(),
            "for": _normalize_duration(cleaned.get("for", "0s")),
            "keepFiringFor": _normalize_duration(cleaned.get("keepFiringFor", "0s")),
            "noDataState": str(cleaned.get("noDataState", "NoData")),
            "execErrState": str(cleaned.get("execErrState", "Error")),
            "paused": bool(cleaned.get("paused", False)),
            "trigger_interval": _normalize_duration(trigger.get("interval", "1m")),
            "expressions": norm_exprs,
            "annotations": _strip_empty_and_defaults(cleaned.get("annotations") or {}),
            "labels": _strip_empty_and_defaults(cleaned.get("labels") or {}),
            "notificationSettings": norm_notif if isinstance(norm_notif, dict) else {},
        }

    return cleaned


def fetch_live_managed_resources(
    gcx_bin: str = "./gcx",
    context: str | None = None,
) -> dict[tuple[str, str], LiveGrafanaResource]:
    """Pobiera zarządzane zasoby z Grafany za pomocą `gcx resources pull`."""
    resources: dict[tuple[str, str], LiveGrafanaResource] = {}
    resolved_bin = resolve_gcx_bin(gcx_bin)
    ctx_flags = ["--context", context] if context else []

    with tempfile.TemporaryDirectory(prefix="gcx-live-state-") as tmp_dir:
        cmd = [
            resolved_bin,
            "resources",
            "pull",
            "folders",
            "dashboards",
            "alertrules",
            "-p",
            tmp_dir,
            "-o",
            "json",
            "--include-managed",
            "--on-error",
            "ignore",
            *ctx_flags,
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=False)

        for json_file in Path(tmp_dir).rglob("*.json"):
            try:
                item = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(item, dict):
                continue

            kind = str(item.get("kind", ""))
            if kind not in ("Folder", "Dashboard", "AlertRule"):
                continue

            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            name = str(meta.get("name", ""))
            if not is_managed_uid(name):
                continue

            annotations = (
                meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
            )
            folder_uid = annotations.get("grafana.app/folder")
            spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
            title = str(spec.get("title", name))

            resources[(kind, name)] = LiveGrafanaResource(
                kind=kind,
                name=name,
                title=title,
                folder_uid=folder_uid,
                spec=canonicalize_spec(kind, spec),
            )

    return resources
