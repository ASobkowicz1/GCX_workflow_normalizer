"""Transformacja surowych zasobów Grafany (Dashboard, AlertRule, Folder) do kanonicznego formatu GCX 1.7."""

import copy
from typing import Any

from live_sync.models import ParsedFile

CANONICAL_ALERT_API_VERSION = "rules.alerting.grafana.app/v0alpha1"


def build_folder_manifest(
    title: str, folder_uid: str, parent_uid: str | None = None
) -> dict[str, Any]:
    """Buduje kanoniczny manifest Folderu (folder.grafana.app/v1beta1)."""
    body: dict[str, Any] = {
        "apiVersion": "folder.grafana.app/v1beta1",
        "kind": "Folder",
        "metadata": {
            "name": folder_uid,
        },
        "spec": {
            "title": title,
            "description": "",
        },
    }
    if parent_uid is not None:
        body["metadata"]["annotations"] = {"grafana.app/folder": parent_uid}
    return body


def normalize_dashboard(parsed: ParsedFile, folder_uid: str, dashboard_uid: str) -> dict[str, Any]:
    """Przekształca surowy eksport Dashboardu do czystego manifestu GCX."""
    if not parsed.data:
        raise ValueError("input data cannot be empty")
    raw = copy.deepcopy(parsed.data)
    if "spec" in raw:
        api_version = raw.get("apiVersion", "dashboard.grafana.app/v2")
        spec = raw["spec"]
    else:
        api_version = "dashboard.grafana.app/v1beta1"
        spec = raw

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
        spec.pop(field, None)

    return {
        "apiVersion": api_version,
        "kind": "Dashboard",
        "metadata": {
            "name": dashboard_uid,
            "annotations": {
                "grafana.app/folder": folder_uid,
            },
        },
        "spec": spec,
    }


def normalize_alertrule(parsed: ParsedFile, folder_uid: str, alert_uid: str) -> dict[str, Any]:
    """Przekształca eksport AlertRule z UI Grafany lub manifest k8s do kanonicznego formatu GCX."""
    if not parsed.data:
        raise ValueError("input data cannot be empty")
    raw = copy.deepcopy(parsed.data)
    target_api_version = CANONICAL_ALERT_API_VERSION
    if "spec" in raw and raw.get("kind") == "AlertRule":
        spec: dict[str, Any] = raw["spec"]
    elif "groups" in raw and raw.get("apiVersion") == 1:
        group = raw["groups"][0]
        rule = group["rules"][0]
        interval = group.get("interval", "1m")
        expressions: dict[str, Any] = {}
        condition_ref = rule.get("condition")

        for item in rule.get("data", []):
            ref_id = item["refId"]
            expr_entry: dict[str, Any] = {"model": item["model"]}
            if item.get("datasourceUid") and item.get("datasourceUid") != "__expr__":
                expr_entry["datasourceUID"] = item["datasourceUid"]
            if item.get("queryType"):
                expr_entry["queryType"] = item["queryType"]
            if "relativeTimeRange" in item:
                rtr = item["relativeTimeRange"]
                from_val = rtr.get("from", 0)
                to_val = rtr.get("to", 0)
                expr_entry["relativeTimeRange"] = {
                    "from": f"{from_val}s" if isinstance(from_val, int) else str(from_val),
                    "to": f"{to_val}s" if isinstance(to_val, int) else str(to_val),
                }
            if ref_id == condition_ref:
                expr_entry["source"] = True
            expressions[ref_id] = expr_entry

        spec = {
            "title": rule["title"],
            "for": rule.get("for", "0s"),
            "noDataState": rule.get("noDataState", "NoData"),
            "execErrState": rule.get("execErrState", "Error"),
            "expressions": expressions,
            "trigger": {"interval": interval},
        }
        for src_key, dst_key in (
            ("annotations", "annotations"),
            ("labels", "labels"),
            ("notification_settings", "notificationSettings"),
            ("keep_firing_for", "keepFiringFor"),
            ("keepFiringFor", "keepFiringFor"),
        ):
            if rule.get(src_key):
                spec[dst_key] = rule[src_key]

        for missing_key in ("missing_series_evals_to_resolve", "missingSeriesEvalsToResolve"):
            if rule.get(missing_key) is not None:
                spec["missingSeriesEvalsToResolve"] = rule[missing_key]
        if rule.get("isPaused"):
            spec["paused"] = True
    else:
        raise ValueError("unsupported alert rule format")

    if "notificationSettings" in spec and isinstance(spec["notificationSettings"], dict):
        if "receiver" in spec["notificationSettings"] and "type" not in spec["notificationSettings"]:
            spec["notificationSettings"]["type"] = "SimplifiedRouting"

    return {
        "apiVersion": target_api_version,
        "kind": "AlertRule",
        "metadata": {
            "name": alert_uid,
            "annotations": {
                "grafana.app/folder": folder_uid,
            },
        },
        "spec": spec,
    }
