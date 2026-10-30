import unicodedata
from pathlib import Path

from normalizer.paths import (
    generate_uid,
    get_canonical_folder_path,
    get_folder_depth,
    slugify_filename,
)


def test_file_directly_in_root_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "readme.md"
    assert get_canonical_folder_path(tmp_path, target) is None
    assert get_folder_depth(None) == 0


def test_root_directory_itself_returns_none(tmp_path: Path) -> None:
    assert get_canonical_folder_path(tmp_path, tmp_path) is None


def test_nested_file_returns_canonical_path_and_depth(tmp_path: Path) -> None:
    target = tmp_path / "PRD - Infrastructure" / "Usage" / "active-users.json"
    canonical = get_canonical_folder_path(tmp_path, target)

    assert canonical == "PRD - Infrastructure/Usage"
    assert get_folder_depth(canonical) == 2


def test_directory_target_returns_its_own_canonical_path(tmp_path: Path) -> None:
    folder = tmp_path / "PRD - Applications" / "Payments"
    folder.mkdir(parents=True)

    canonical = get_canonical_folder_path(tmp_path, folder)
    assert canonical == "PRD - Applications/Payments"
    assert get_folder_depth(canonical) == 2


def test_path_outside_root_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "other" / "dash.json"

    assert get_canonical_folder_path(root, outside) is None


def test_unicode_normalization_nfc(tmp_path: Path) -> None:
    # NFD (rozbite znaki diakrytyczne, częste na macOS) -> powinno znormalizować do NFC
    nfd_name = unicodedata.normalize("NFD", "Środowisko")
    target = tmp_path / nfd_name / "Usługi" / "dash.json"

    canonical = get_canonical_folder_path(tmp_path, target)
    assert canonical == unicodedata.normalize("NFC", "Środowisko/Usługi")
    assert get_folder_depth(canonical) == 2


def test_folder_with_dot_in_name_existing_and_non_existing(tmp_path: Path) -> None:
    # 1. Katalog z kropką, który jeszcze nie istnieje na dysku
    non_existing_folder = tmp_path / "PRD - Applications" / "Release 2.0"
    assert (
        get_canonical_folder_path(tmp_path, non_existing_folder) == "PRD - Applications/Release 2.0"
    )

    # 2. Katalog z kropką istniejący na dysku
    non_existing_folder.mkdir(parents=True)
    assert (
        get_canonical_folder_path(tmp_path, non_existing_folder) == "PRD - Applications/Release 2.0"
    )

    # 3. Plik wewnątrz katalogu z kropką
    file_inside = non_existing_folder / "dashboard.json"
    assert get_canonical_folder_path(tmp_path, file_inside) == "PRD - Applications/Release 2.0"


def test_generate_uid_deterministic_and_collision_resistant() -> None:
    # Różne struktury katalogów z draftu: "Cos tam" vs "Cos/tam" muszą dać różne UID
    uid1 = generate_uid("f", "Infra/Azure/Cos tam")
    uid2 = generate_uid("f", "Infra/Azure/Cos/tam")

    assert uid1 != uid2
    assert uid1.startswith("f-")
    assert len(uid1) == 22  # "f-" (2) + 20 hex chars


def test_slugify_filename() -> None:
    slug1 = slugify_filename("Dashboard Zbycha z infry", ".json")
    slug2 = slugify_filename("Alert CPU / RAM")
    slug3 = slugify_filename("Series active", ".yaml", prefix="d")

    assert slug1 == "dashboard-zbycha-z-infry.json"
    assert slug2 == "alert-cpu-ram.yaml"
    assert slug3 == "d-series-active.yaml"
