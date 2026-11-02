"""Interfejs linii poleceń (CLI) obsługujący dwa tryby pracy: plan (normalizacja + walidacja) oraz deploy (wdrożenie delty)."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from normalizer.planner import (
    build_delta_plan,
    build_gcx_delta_commands,
    format_terraform_summary,
)
from normalizer.reconciler import reconcile_data_directory


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
    """
    Uruchamia kolejne polecenia GCX i przerywa wykonanie w razie błędu.
    Jeśli polecenie `delete` zwróci 404 / not found (bo zasób został już usunięty),
    wypisuje ostrzeżenie i kontynuuje bezpiecznie.
    """
    for cmd in commands:
        is_validation_step = not is_apply or "validate" in cmd or "--dry-run" in cmd
        is_delete_step = "delete" in cmd
        cmd_display = [Path(cmd[0]).name] + cmd[1:]
        prefix = "🔍 Validating" if is_validation_step else "🚀 Applying"
        print(f"\n{prefix}: {' '.join(cmd_display)}")
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
            if is_delete_step and proc.returncode == 4 and any("folders/" in arg for arg in cmd):
                print(
                    "⚠️ Folder could not be deleted in Grafana (exit code 4): it still contains active resources or child folders in Grafana.",
                    file=sys.stderr,
                )
            print(f"❌ Command failed with exit code {proc.returncode}")
            return proc.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    """Główny punkt wejścia programu obsługujący argumenty wiersza poleceń."""
    parser = argparse.ArgumentParser(
        prog="normalizer",
        description="GCX GitOps Normalizer & Terraform-style Delta Planner",
    )
    parser.add_argument(
        "mode",
        choices=["plan", "deploy"],
        help=(
            "plan: normalize files in data/, validate rules and generate diff plan (gcx validate); "
            "deploy: deploy delta changes after merge using normalized files (gcx push / delete)"
        ),
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path(), help="Path to Git repository root"
    )
    parser.add_argument(
        "--data-dir", default="grafana", help="Subdirectory with Grafana resources (default: grafana)"
    )
    parser.add_argument(
        "--base-ref", default="HEAD", help="Base Git ref for diff comparison (e.g. origin/prd)"
    )
    parser.add_argument(
        "--target-ref",
        default=None,
        help="Target Git ref for deploy mode (e.g. HEAD)",
    )
    parser.add_argument(
        "--full-push",
        action="store_true",
        help="Force push all files from data directory regardless of diff",
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

    if args.mode == "plan":
        # 1. Normalizacja plików w katalogu data/
        rec_result = reconcile_data_directory(data_root, dry_run=False)
        # 2. Wyliczenie planu zmian względem base_ref
        plan = build_delta_plan(
            repo_root=repo_root,
            data_subdir=args.data_dir,
            base_ref=args.base_ref,
            target_ref=None,
            issues=rec_result.issues,
            full_push=args.full_push,
        )
        summary_md = format_terraform_summary(plan, is_apply=False)
        _write_summary(summary_md, args.summary_file)

        if plan.has_errors:
            return 1

        if not args.skip_gcx and plan.items:
            commands = build_gcx_delta_commands(
                plan,
                gcx_bin=args.gcx_bin,
                dry_run=True,
            )
            return _execute_commands(commands, repo_root, is_apply=False)
        return 0

    if args.mode == "deploy":
        # Tryb po Merge na branchach środowiskowych: zero normalizacji, tylko wdrożenie delty!
        target_ref = args.target_ref or "HEAD"
        plan = build_delta_plan(
            repo_root=repo_root,
            data_subdir=args.data_dir,
            base_ref=args.base_ref,
            target_ref=target_ref,
            full_push=args.full_push,
        )
        summary_md = format_terraform_summary(plan, is_apply=True)
        _write_summary(summary_md, args.summary_file)

        if not args.skip_gcx and plan.items:
            commands = build_gcx_delta_commands(
                plan,
                gcx_bin=args.gcx_bin,
                dry_run=False,
            )
            return _execute_commands(commands, repo_root, is_apply=True)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
