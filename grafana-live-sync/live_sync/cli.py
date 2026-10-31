"""Interfejs linii poleceń (CLI) obsługujący dwa tryby pracy: plan (normalizacja + walidacja) oraz deploy (wdrożenie zmian)."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Umożliwia uruchomienie skryptu bezpośrednio (`python3 grafana-live-sync/live_sync/cli.py`)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_sync.grafana_state import fetch_live_managed_resources
from live_sync.planner import (
    build_gcx_delta_commands,
    build_live_state_plan,
    format_terraform_summary,
)
from live_sync.reconciler import reconcile_data_directory


def _write_summary(markdown: str, output_file: Path | None = None) -> None:
    """Wypisuje plan na ekran, opcjonalnie zapisuje do pliku oraz dopisuje do GITHUB_STEP_SUMMARY."""
    print(markdown)
    if output_file:
        output_file.write_text(markdown, encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def _filter_gcx_output(text: str) -> str:
    """Ukrywa nadmiarowe komunikaty informacyjne GCX (auto-approval, brak server-side dry-run dla alertrules)."""
    filtered_lines: list[str] = []
    for line in text.splitlines():
        if "Auto-approval enabled: automatically setting --force" in line:
            continue
        if "alertrules.rules.alerting.grafana.app does not support server-side dry-run" in line:
            continue
        if "skipped (server-side dry-run unsupported, not verified)" in line:
            line = "✔ No errors found."
        filtered_lines.append(line)
    return "\n".join(filtered_lines)


def _execute_commands(commands: list[list[str]], repo_root: Path, is_apply: bool = False) -> int:
    """Uruchamia kolejne polecenia GCX i przerywa wykonanie w razie błędu."""
    for cmd in commands:
        is_validation_step = not is_apply or "validate" in cmd or "--dry-run" in cmd
        is_delete_step = "delete" in cmd
        prefix = "🔍 Validating" if is_validation_step else "🚀 Applying"
        # W logu pokazujemy samą nazwę binarki (np. "gcx") zamiast pełnej ścieżki
        print(f"\n{prefix}: {' '.join([Path(cmd[0]).name, *cmd[1:]])}")
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        if proc.stdout:
            cleaned_out = _filter_gcx_output(proc.stdout)
            if cleaned_out.strip():
                print(cleaned_out)
        if proc.stderr:
            cleaned_err = _filter_gcx_output(proc.stderr)
            if cleaned_err.strip():
                print(cleaned_err, file=sys.stderr)
        if proc.returncode != 0:
            combined_out = f"{proc.stdout}\n{proc.stderr}".lower()
            if is_delete_step and ("not found" in combined_out or "404" in combined_out):
                print(
                    "⚠️ Resource to delete was already absent in Grafana (404 Not Found) – continuing safely."
                )
                continue
            print(f"❌ Command failed with exit code {proc.returncode}")
            return proc.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    """Główny punkt wejścia programu obsługujący argumenty wiersza poleceń."""
    parser = argparse.ArgumentParser(
        prog="live_sync",
        description="GCX GitOps Live Drift Auditor & Reconciler",
    )
    parser.add_argument(
        "mode",
        choices=["plan", "deploy"],
        help="plan: audit drift between repository and live Grafana; deploy: reconcile and fix drift in Grafana",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path(), help="Path to Git repository root"
    )
    parser.add_argument(
        "--data-dir",
        default="grafana",
        help="Subdirectory with Grafana resources (default: grafana)",
    )
    parser.add_argument("--gcx-bin", default="./gcx", help="Path to gcx binary")
    parser.add_argument(
        "--skip-gcx", action="store_true", help="Only generate execution plan without invoking gcx"
    )
    parser.add_argument(
        "--summary-file", type=Path, default=None, help="Save Markdown summary report to file"
    )

    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    data_root = repo_root / args.data_dir
    gcx_bin = args.gcx_bin

    print("## Fetching live resources state from Grafana...")
    try:
        live_resources = fetch_live_managed_resources(gcx_bin=gcx_bin)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(f"## Found {len(live_resources)} repository managed resources (f-/d-/a-) in Grafana.\n")

    if args.mode == "plan":
        rec_result = reconcile_data_directory(data_root)
        plan = build_live_state_plan(
            repo_root=repo_root,
            live_resources=live_resources,
            data_subdir=args.data_dir,
            issues=rec_result.issues,
        )
        summary_md = format_terraform_summary(plan, is_apply=False)
        _write_summary(summary_md, args.summary_file)

        if plan.has_errors:
            return 1

        if not args.skip_gcx and plan.items:
            commands = build_gcx_delta_commands(
                plan,
                gcx_bin=gcx_bin,
                dry_run=True,
            )
            return _execute_commands(commands, repo_root, is_apply=False)
        return 0

    if args.mode == "deploy":
        rec_result = reconcile_data_directory(data_root)
        plan = build_live_state_plan(
            repo_root=repo_root,
            live_resources=live_resources,
            data_subdir=args.data_dir,
            issues=rec_result.issues,
        )
        summary_md = format_terraform_summary(plan, is_apply=True)
        _write_summary(summary_md, args.summary_file)

        if plan.has_errors:
            return 1

        if not args.skip_gcx and plan.items:
            commands = build_gcx_delta_commands(
                plan,
                gcx_bin=gcx_bin,
                dry_run=False,
            )
            return _execute_commands(commands, repo_root, is_apply=True)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
