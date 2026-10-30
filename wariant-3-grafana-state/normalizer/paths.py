"""Funkcje odpowiedzialne za kanonizację ścieżek, wyliczanie głębokości drzewa, generowanie UID i nazw plików."""

import hashlib
import re
import unicodedata
from pathlib import Path


def get_canonical_folder_path(root_path: Path, target_path: Path) -> str | None:
    """
    Wyznacza kanoniczną ścieżkę folderu Grafany względem katalogu głównego `data/`.
    Zwraca None dla samego katalogu głównego, plików leżących bezpośrednio w `data/`
    oraz ścieżek znajdujących się poza `data/`.
    """
    root_path = root_path.resolve()
    target_path = target_path.resolve()
    if root_path == target_path or not root_path.exists():
        return None

    known_file_extensions = {".json", ".yaml", ".yml", ".md", ".txt", ".prom", ".tf"}
    if target_path.exists():
        is_file = target_path.is_file()
    else:
        is_file = target_path.suffix.lower() in known_file_extensions

    folder_path = target_path.parent if is_file else target_path
    if folder_path == root_path or not folder_path.is_relative_to(root_path):
        return None

    relative_path = folder_path.relative_to(root_path).as_posix()
    relative_path_normalized = unicodedata.normalize("NFC", relative_path)
    return relative_path_normalized


def get_folder_depth(canonical_folder_path: str | None) -> int:
    """Zwraca poziom zagnieżdżenia folderu w Grafanie (1 = folder główny, max 4 w Grafanie)."""
    if not canonical_folder_path:
        return 0
    return canonical_folder_path.count("/") + 1


def generate_uid(prefix: str, canonical_folder_path: str | None, title: str | None = None) -> str:
    """
    Generuje deterministyczny, 22-znakowy identyfikator UID zasobu (np. f-<20_hex>, d-<20_hex>, a-<20_hex>)
    na podstawie kanonicznej ścieżki folderu oraz (opcjonalnie) tytułu zasobu.
    """
    if not canonical_folder_path:
        raise ValueError("canonical_folder_path cannot be empty")
    identity = f"{canonical_folder_path}::{title.strip()}" if title else canonical_folder_path
    normalized_identity = unicodedata.normalize("NFC", identity)
    hashed_identity = hashlib.sha256(normalized_identity.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{hashed_identity}"


def slugify_filename(
    title: str,
    suffix: str = ".yaml",
    prefix: str | None = None,
) -> str:
    """
    Przekształca tytuł zasobu na bezpieczną nazwę pliku (małe litery, bez polskich znaków, słowa oddzielone '-').
    Jeśli podano `prefix` (np. 'd' dla dashboardu lub 'a' dla alertu), dodaje go na początku: `d-<slug>.yaml`.
    """
    title_lower = title.lower()
    title_normalized = (
        unicodedata.normalize("NFKD", title_lower.translate(str.maketrans({"ł": "l", "Ł": "l"})))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", title_normalized).strip("-")
    if not slug:
        raise ValueError(f"cannot generate valid filename slug from title: {title!r}")
    if prefix:
        return f"{prefix}-{slug}{suffix}"
    return f"{slug}{suffix}"
