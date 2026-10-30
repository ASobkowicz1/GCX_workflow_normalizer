"""Modele danych (dataclasses oraz wyliczenia Enum) używane w całym procesie normalizacji i planowania."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class FileKind(StrEnum):
    """Kategoria rozpoznanego pliku w katalogu data/."""

    CANONICAL_FOLDER = "canonical_folder"
    CANONICAL_DASHBOARD = "canonical_dashboard"
    CANONICAL_ALERTRULE = "canonical_alertrule"
    RAW_UI_DASHBOARD = "raw_ui_dashboard"
    RAW_UI_ALERTRULE = "raw_ui_alertrule"
    IGNORED = "ignored"
    INVALID = "invalid"


class IssueSeverity(StrEnum):
    """Poziom istotności problemu wykrytego podczas walidacji."""

    ERROR = "ERROR"
    WARNING = "WARNING"


class PlanActionType(StrEnum):
    """Typ operacji w planie wdrożenia (odpowiednik akcji z Terraform Plan)."""

    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"


@dataclass
class ParsedFile:
    """Reprezentacja pojedynczego pliku po wczytaniu i wstępnej klasyfikacji przez parser."""

    path: Path
    kind: FileKind
    data: dict[str, Any] | None = None
    title: str | None = None
    error: str | None = None


@dataclass
class ValidationIssue:
    """Pojedynczy błąd lub ostrzeżenie walidacji (np. przekroczenie głębokości folderów, kolizja nazw)."""

    path: Path
    severity: IssueSeverity
    message: str


@dataclass
class PlanItem:
    """Pojedynczy element planu zmian wyliczony na podstawie git diff."""

    action: PlanActionType
    kind: str  # "Folder", "Dashboard", "AlertRule"
    name: str  # metadata.name (deterministyczny UID zasobu w GCX: f-..., d-..., a-...)
    title: str
    canonical_folder: str
    repo_path: Path
    depth: int = 1


@dataclass
class ExecutionPlan:
    """Kompletny plan wdrożenia zmian (Delta) wraz z listą ostrzeżeń/błędów walidacji."""

    items: list[PlanItem] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def to_add(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == PlanActionType.CREATE]

    @property
    def to_change(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == PlanActionType.UPDATE]

    @property
    def to_destroy(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == PlanActionType.DESTROY]

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == IssueSeverity.ERROR for issue in self.issues)


@dataclass
class ReconcileResult:
    """Podsumowanie pracy modułu reconciler na katalogu data/."""

    written_files: list[Path] = field(default_factory=list)
    deleted_raw_files: list[Path] = field(default_factory=list)
    deleted_empty_folders: list[Path] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == IssueSeverity.ERROR for issue in self.issues)
