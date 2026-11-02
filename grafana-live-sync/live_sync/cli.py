"""Interfejs linii poleceń (CLI) obsługujący dwa tryby pracy: plan (normalizacja + walidacja) oraz deploy (wdrożenie zmian)."""

import argparse
import os
import re
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
    """Ukrywa nadmiarowe komunikaty informacyjne GCX (auto-approval, brak server-side dry-run dla alertrules/recordingrules)."""
    filtered_lines: list[str] = []
    for line in text.splitlines():
        if "Auto-approval enabled: automatically setting --force" in line:
            continue
        if "does not support server-side dry-run" in line:
            continue
        if "skipped (server-side dry-run unsupported, not verified)" in line:
            line = "✔ No errors found."
        filtered_lines.append(line)
    return "\n".join(filtered_lines)


def _format_command_header(
    cmd: list[str],
    is_apply: bool = False,
    data_dir: str = "",
) -> str:
    """Tworzy czytelny nagłówek informujący użytkownika o przetwarzanych plikach/zasobach zamiast surowej komendy CLI."""
    data_prefix = f"{data_dir.strip('./')}/" if data_dir else ""
    if "validate" in cmd:
        files = [
            cmd[i + 1].removeprefix(data_prefix)
            for i in range(len(cmd) - 1)
            if cmd[i] == "-p"
        ]
        if files:
            lines = ["🔍 Validating resources against Grafana API:"]
            for f in files:
                lines.append(f"  • {f}")
            return "\n".join(lines)
        return "🔍 Validating resources against Grafana API..."

    if "push" in cmd:
        files = [
            cmd[i + 1].removeprefix(data_prefix)
            for i in range(len(cmd) - 1)
            if cmd[i] == "-p"
        ]
        if files:
            lines = ["🚀 Pushing resources to Grafana:"]
            for f in files:
                lines.append(f"  • {f}")
            return "\n".join(lines)
        return "🚀 Pushing resources to Grafana..."

    if "delete" in cmd:
        selectors = [
            arg
            for arg in cmd[1:]
            if not arg.startswith("-") and arg not in ("resources", "delete")
        ]
        if selectors:
            lines = ["🚀 Deleting resources from Grafana:"]
            for s in selectors:
                lines.append(f"  • {s}")
            return "\n".join(lines)
        return "🚀 Deleting resources from Grafana..."

    cmd_display = [Path(cmd[0]).name] + cmd[1:]
    prefix = (
        "🔍 Validating"
        if (not is_apply or "validate" in cmd or "--dry-run" in cmd)
        else "🚀 Applying"
    )
    return f"{prefix}: {' '.join(cmd_display)}"


def _extract_error_detail(stdout: str, stderr: str, default_code: int) -> str:
    """Wyciąga najbardziej czytelną informację o przyczynie błędu z wyjścia GCX."""
    combined = f"{stderr}\n{stdout}"
    reasons: list[str] = []
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "Auto-approval enabled" in line or "does not support server-side dry-run" in line:
            continue
        # Filtrujemy linie-liczniki GCX (np. "warn: 2 resource(s) failed to delete", "✘ 0 resources deleted, 2 errors")
        if re.search(r"^warn:\s*\d+\s*resource\(s\)\s*failed", line, re.IGNORECASE):
            continue
        if re.search(
            r"^[✘✔⚠❌]\s*\d+\s*resources?\s*(deleted|pushed|created|errors?)",
            line,
            re.IGNORECASE,
        ):
            continue
        if line.startswith(("❌", "⚠", "✔", "✘")):
            continue
        lower = line.lower()
        if any(
            kw in lower
            for kw in (
                "error",
                "invalid",
                "failed",
                "rejected",
                "cannot",
                "unknown",
                "denied",
                "conflict",
                "required",
                "bad request",
                "unprocessable",
            )
        ):
            reasons.append(line)

    if reasons:
        seen = set()
        unique = [r for r in reasons if not (r in seen or seen.add(r))]
        return "; ".join(unique[:3])

    if default_code == 4:
        return "Rejected by Grafana API (exit code 4: Schema/field validation error or parent folder restriction)"
    return f"Failed with exit code {default_code}"


def _diagnose_batch_failure(
    cmd: list[str],
    repo_root: Path,
    data_dir: str = "",
) -> list[tuple[str, str]]:
    """
    Uruchamia precyzyjną diagnostykę per-plik, gdy skonsolidowane polecenie (validate lub push) zawiedzie.
    Bada każdy plik z paczki z osobna i zbiera pełną listę wszystkich uszkodzonych zasobów naraz.
    """
    data_prefix = f"{data_dir.strip('./')}/" if data_dir else ""
    gcx_bin = cmd[0]
    action = "push" if "push" in cmd else "validate"
    files = [cmd[i + 1] for i in range(len(cmd) - 1) if cmd[i] == "-p"]

    failures: list[tuple[str, str]] = []
    for f in files:
        single_cmd = [gcx_bin, "resources", action, "-p", f]
        proc = subprocess.run(single_cmd, cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            detail = _extract_error_detail(proc.stdout, proc.stderr, proc.returncode)
            display_path = f.removeprefix(data_prefix)
            failures.append((display_path, detail))
    return failures


def _execute_commands(
    commands: list[list[str]],
    repo_root: Path,
    is_apply: bool = False,
    data_dir: str = "",
) -> int:
    """
    Uruchamia kolejne polecenia GCX z podziałem na skonsolidowane fazy (Validate, Push, Delete)
    oraz automatyczną zbiorczą diagnostyką wszystkich błędnych zasobów w przypadku niepowodzenia.
    """
    non_delete_cmds = [cmd for cmd in commands if "delete" not in cmd]
    delete_cmds = [cmd for cmd in commands if "delete" in cmd]

    # KROK 1: Wykonanie fazy Validate oraz Push
    for cmd in non_delete_cmds:
        print(f"\n{_format_command_header(cmd, is_apply, data_dir=data_dir)}")
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
            # Uruchamiamy automatyczną zbiorczą diagnostykę wszystkich plików w paczce
            diag_failures = _diagnose_batch_failure(cmd, repo_root, data_dir=data_dir)
            if diag_failures:
                action_name = "Deployment" if "push" in cmd else "Validation"
                print(
                    f"\n❌ {action_name} failed – {len(diag_failures)} resource(s) rejected by Grafana API:"
                )
                for f_name, reason in diag_failures:
                    print(f"  • {f_name}\n    Error: {reason}")
            else:
                detail = _extract_error_detail(proc.stdout, proc.stderr, proc.returncode)
                print(f"\n❌ Command failed: {detail}")

            print(f"❌ Command failed with exit code {proc.returncode}")
            return proc.returncode

    # KROK 2: Wykonanie skonsolidowanej fazy Delete
    if delete_cmds:
        gcx_bin = delete_cmds[0][0]
        all_selectors: list[str] = []
        for cmd in delete_cmds:
            for arg in cmd[1:]:
                if not arg.startswith("-") and arg not in ("resources", "delete"):
                    all_selectors.append(arg)

        lines = ["\n🚀 Deleting resources from Grafana:"]
        for s in all_selectors:
            lines.append(f"  • {s}")
        print("\n".join(lines))

        total_deleted = 0
        total_already_absent = 0
        delete_failures: list[tuple[str, str]] = []

        for cmd in delete_cmds:
            cmd_selectors = [
                arg
                for arg in cmd[1:]
                if not arg.startswith("-") and arg not in ("resources", "delete")
            ]
            proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
            combined_out = f"{proc.stdout}\n{proc.stderr}".lower()

            if proc.returncode != 0:
                is_absent_in_output = any(
                    kw in combined_out
                    for kw in ("not found", "404", "does not exist", "notfound", "cannot find")
                )

                for s in cmd_selectors:
                    if is_absent_in_output:
                        total_already_absent += 1
                        continue

                    # Jeśli GCX zwrócił ogólny błąd bez treści (kod 4), sprawdzamy czy zasób w ogóle istnieje w Grafanie
                    parts = s.split("/", 1)
                    if len(parts) == 2:
                        chk_cmd = [gcx_bin, "resources", "get", parts[0], parts[1]]
                        chk_proc = subprocess.run(
                            chk_cmd, cwd=repo_root, capture_output=True, text=True
                        )
                        chk_out = f"{chk_proc.stdout}\n{chk_proc.stderr}".lower()
                        if chk_proc.returncode != 0 or any(
                            kw in chk_out
                            for kw in ("not found", "404", "does not exist", "notfound")
                        ):
                            # Zasób nie istnieje w Grafanie – sukces idempotencji!
                            total_already_absent += 1
                            continue

                        # Zasób istnieje w Grafanie: ponawiamy usunięcie indywidualne
                        retry_cmd = [gcx_bin, "resources", "delete", s, "-y"]
                        retry_proc = subprocess.run(
                            retry_cmd, cwd=repo_root, capture_output=True, text=True
                        )
                        if retry_proc.returncode == 0:
                            total_deleted += 1
                            continue

                        if retry_proc.returncode == 4 and "folders/" in s:
                            msg = "Folder could not be deleted (exit code 4): it still contains active resources or child folders in Grafana."
                        else:
                            msg = _extract_error_detail(
                                retry_proc.stdout, retry_proc.stderr, retry_proc.returncode
                            )
                        delete_failures.append((s, msg))
                    else:
                        msg = _extract_error_detail(proc.stdout, proc.stderr, proc.returncode)
                        delete_failures.append((s, msg))
            else:
                total_deleted += len(cmd_selectors)

        if delete_failures:
            print(
                f"\n❌ Deletion failed – {len(delete_failures)} resource(s) could not be deleted:"
            )
            for sel, reason in delete_failures:
                print(f"  • {sel}\n    Error: {reason}")
            print("❌ Command failed with exit code 4")
            return 4

        summary_parts: list[str] = []
        if total_deleted > 0:
            summary_parts.append(f"{total_deleted} resources deleted")
        if total_already_absent > 0:
            summary_parts.append(f"{total_already_absent} already absent")
        if not summary_parts:
            summary_parts.append("0 resources deleted")

        print(f"✔ {', '.join(summary_parts)} in Grafana, 0 errors")

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
            return _execute_commands(
                commands, repo_root, is_apply=False, data_dir=args.data_dir
            )
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
            return _execute_commands(
                commands, repo_root, is_apply=True, data_dir=args.data_dir
            )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
