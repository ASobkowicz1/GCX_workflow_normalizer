"""Transformacja surowych zasobów Grafany (Dashboard, AlertRule, Folder) do kanonicznego formatu GCX 1.7."""

import copy
from typing import Any

from normalizer.models import ParsedFile

CANONICAL_ALERT_API_VERSION = "rules.alerting.grafana.app/v0alpha1"


def build_folder_manifest(
    title: str, folder_uid: str, parent_uid: str | None = None
) -> dict[str, Any]:
    """
    Buduje kanoniczny manifest Folderu (folder.grafana.app/v1beta1).
    Dla folderów nadrzędnych (poziom 1) pomija adnotację `grafana.app/folder`, dzięki czemu trafiają do Root/General.
    Dla podfolderów (poziomy 2..4) wskazuje UID rodzica w `metadata.annotations['grafana.app/folder']`.
    """
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
    """
    Przekształca surowy eksport Dashboardu (v2 k8s lub klasyczny v1 JSON) do czystego manifestu GCX.
    Usuwa wszystkie pola specyficzne dla instancji źródłowej (namespace, uid, resourceVersion, folderUid)
    i ustawia docelowe `metadata.name` (d-...) oraz referencję do folderu `grafana.app/folder` (f-...).
    """
    if not parsed.data:
        raise ValueError("input data cannot be empty")
    raw = copy.deepcopy(parsed.data)
    if "spec" in raw:
        api_version = raw.get("apiVersion", "dashboard.grafana.app/v2")
        spec = raw["spec"]
    else:
        api_version = "dashboard.grafana.app/v1beta1"
        spec = raw

    # Czyszczenie pól środowiskowych z definicji dashboardu
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
    """
    Przekształca eksport AlertRule z UI Grafany (apiVersion: 1, groups -> rules) lub manifest k8s
    do kanonicznego formatu GCX (`rules.alerting.grafana.app/v0alpha1`).
    """
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

        # Konwersja tablicy zapytań `data` z eksportu UI na słownik `expressions` wymagany przez API k8s
        for item in rule.get("data", []):
            ref_id = item["refId"]
            expr_entry: dict[str, Any] = {"model": item["model"]}
            if item.get("datasourceUid") and item.get("datasourceUid") != "__expr__":
                expr_entry["datasourceUID"] = item["datasourceUid"]
            if item.get("queryType"):
                expr_entry["queryType"] = item["queryType"]
            if "relativeTimeRange" in item:
                rtr = item["relativeTimeRange"]
                # UI eksportuje sekundy jako liczby całkowite (np. 600), a API k8s oczekuje stringów (np. "600s")
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
        if (
            "receiver" in spec["notificationSettings"]
            and "type" not in spec["notificationSettings"]
        ):
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


def normalize_recordingrule(
    parsed: ParsedFile, folder_uid: str, recordingrule_uid: str
) -> dict[str, Any]:
    """
    Normalizuje surowy eksport RecordingRule lub plik pobrany z UI
    do kanonicznego formatu GCX (`rules.alerting.grafana.app/v0alpha1`).
    """
    if not parsed.data:
        raise ValueError("input data cannot be empty")
    raw = copy.deepcopy(parsed.data)
    target_api_version = CANONICAL_ALERT_API_VERSION
    if "spec" in raw and raw.get("kind") == "RecordingRule":
        spec: dict[str, Any] = raw["spec"]
    elif "groups" in raw and raw.get("apiVersion") == 1:
        group = raw["groups"][0]
        rule = group["rules"][0]
        interval = group.get("interval", "1m")
        expressions: dict[str, Any] = {}
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
            expressions[ref_id] = expr_entry

        rec_obj = (
            rule.get("record")
            if isinstance(rule.get("record"), dict)
            else (rule.get("metric") if isinstance(rule.get("metric"), dict) else {})
        )
        target_ds_rule = (
            rule.get("targetDatasourceUid")
            or rule.get("targetDatasourceUID")
            or rec_obj.get("targetDatasourceUid")
            or rec_obj.get("targetDatasourceUID")
            or ""
        )
        from_ref = rec_obj.get("from") or rec_obj.get("refId")

        metric_name = (
            rec_obj.get("metric")
            or rec_obj.get("name")
            or (rule.get("record") if isinstance(rule.get("record"), str) else None)
            or (rule.get("metric") if isinstance(rule.get("metric"), str) else None)
            or (rule.get("title") if isinstance(rule.get("title"), str) else None)
            or "recording-rule"
        )
        title = (rule.get("title") if isinstance(rule.get("title"), str) else None) or metric_name

        spec = {
            "title": title,
            "metric": metric_name,
            "targetDatasourceUID": target_ds_rule,
            "expressions": expressions,
            "trigger": {"interval": interval},
        }
        if from_ref:
            spec["from"] = from_ref
        if rule.get("labels"):
            spec["labels"] = rule["labels"]
        if rule.get("paused") or rule.get("isPaused"):
            spec["paused"] = True
    else:
        raise ValueError("unsupported recording rule format")

    raw_metric = spec.get("metric")
    metric_name = ""
    target_ds = (
        spec.get("targetDatasourceUID")
        or spec.get("target_datasource_uid")
        or spec.get("targetDatasourceUid")
        or ""
    )
    from_ref = spec.get("from")

    if isinstance(raw_metric, dict):
        metric_name = (
            raw_metric.get("metric")
            or raw_metric.get("name")
            or str(spec.get("title", "recording-rule"))
        )
        if not target_ds:
            target_ds = (
                raw_metric.get("targetDatasourceUid")
                or raw_metric.get("targetDatasourceUID")
                or raw_metric.get("target_datasource_uid")
                or ""
            )
        if not from_ref:
            from_ref = raw_metric.get("from") or raw_metric.get("refId")
    elif isinstance(raw_metric, str) and raw_metric:
        metric_name = raw_metric
    elif spec.get("title"):
        metric_name = str(spec.get("title"))
    else:
        metric_name = "recording-rule"

    expressions = spec.get("expressions", {})
    if isinstance(expressions, dict):
        if from_ref and from_ref in expressions and isinstance(expressions[from_ref], dict):
            expressions[from_ref]["source"] = True
        elif (
            not any(
                isinstance(expr, dict) and expr.get("source") is True
                for expr in expressions.values()
            )
            and len(expressions) == 1
        ):
            only_expr = next(iter(expressions.values()))
            if isinstance(only_expr, dict):
                only_expr["source"] = True

    title = spec.get("title")
    if not title or not isinstance(title, str):
        title = metric_name

    trigger = spec.get("trigger")
    if not isinstance(trigger, dict):
        trigger = {"interval": "1m"}
    elif "interval" not in trigger:
        trigger["interval"] = "1m"

    cleaned_spec: dict[str, Any] = {
        "title": title,
        "metric": metric_name,
        "targetDatasourceUID": target_ds,
        "expressions": expressions,
        "trigger": trigger,
    }
    if spec.get("labels"):
        cleaned_spec["labels"] = spec["labels"]
    if spec.get("paused") is not None:
        cleaned_spec["paused"] = spec["paused"]

    return {
        "apiVersion": target_api_version,
        "kind": "RecordingRule",
        "metadata": {
            "name": recordingrule_uid,
            "annotations": {
                "grafana.app/folder": folder_uid,
            },
        },
        "spec": cleaned_spec,
    }
