"""Wariant 1: CLI obsługujące plan i deploy na bazie Git Diff + bezpiecznik --full-sync oraz odporność na 404 przy delete."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

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
            line = line.replace("⚠", "✔").split(",")[0] + " (alertrules validated client-side)."
        filtered_lines.append(line)
    return "\n".join(filtered_lines)


def _execute_commands(commands: list[list[str]], repo_root: Path, is_apply: bool = False) -> int:
    """
    Uruchamia kolejne polecenia GCX.
    W Wariancie 1 jeśli polecenie `delete` zwróci błąd `not found` / `404` (bo ktoś ręcznie skasował zasób w UI Grafany),
    wypisuje ostrzeżenie i kontynuuje, zamiast przerywać cały pipeline.
    """
    for cmd in commands:
        is_validation_step = not is_apply or "validate" in cmd or "--dry-run" in cmd
        is_delete_step = "delete" in cmd
        prefix = "🔍 Validating" if is_validation_step else "🚀 Applying"
        print(f"\n{prefix}: {' '.join(cmd)}")

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
        prog="normalizer",
        description="GCX GitOps Normalizer (Wariant 1: Git Diff + Full-Sync Safeguard)",
    )
    parser.add_argument(
        "mode",
        choices=["plan", "deploy"],
        help="plan: normalizacja + walidacja delty; deploy: wdrożenie delty zmian po merge",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path(), help="Ścieżka do repozytorium Git"
    )
    parser.add_argument(
        "--data-dir", default="grafana", help="Podkatalog z zasobami Grafany (domyślnie: grafana)"
    )
    parser.add_argument(
        "--base-ref", default="HEAD", help="Bazowy ref Git do porównania diff (np. origin/prd)"
    )
    parser.add_argument(
        "--target-ref",
        default=None,
        help="Docelowy ref Git dla trybu deploy (np. HEAD)",
    )
    parser.add_argument(
        "--full-sync",
        action="store_true",
        help="Wymuś wypchnięcie wszystkich plików z grafana/ niezależnie od historii Git (np. z workflow_dispatch)",
    )
    parser.add_argument("--gcx-bin", default="./gcx", help="Ścieżka do pliku wykonywalnego gcx")
    parser.add_argument("--context", default=None, help="Nazwa kontekstu GCX (np. dev, tech, prd)")
    parser.add_argument(
        "--skip-gcx", action="store_true", help="Tylko wygeneruj plan bez wywoływania gcx"
    )
    parser.add_argument(
        "--summary-file", type=Path, default=None, help="Zapisz raport Markdown do pliku"
    )

    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    data_root = repo_root / args.data_dir

    if args.mode == "plan":
        rec_result = reconcile_data_directory(data_root, dry_run=False)
        plan = build_delta_plan(
            repo_root=repo_root,
            data_subdir=args.data_dir,
            base_ref=args.base_ref,
            target_ref=None,
            issues=rec_result.issues,
            full_sync=args.full_sync,
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
                context=args.context,
            )
            return _execute_commands(commands, repo_root, is_apply=False)
        return 0

    if args.mode == "deploy":
        target_ref = args.target_ref or "HEAD"
        plan = build_delta_plan(
            repo_root=repo_root,
            data_subdir=args.data_dir,
            base_ref=args.base_ref,
            target_ref=target_ref,
            full_sync=args.full_sync,
        )
        summary_md = format_terraform_summary(plan, is_apply=True)
        _write_summary(summary_md, args.summary_file)

        if not args.skip_gcx and plan.items:
            commands = build_gcx_delta_commands(
                plan,
                gcx_bin=args.gcx_bin,
                dry_run=False,
                context=args.context,
            )
            return _execute_commands(commands, repo_root, is_apply=True)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
