# GCX Workflow Normalizer & Delta Planner (Dokumentacja LLD)

## 1. Cel i zasada działania
Narzędzie `normalizer` pełni rolę kompilatora i planera GitOps pomiędzy surowymi eksportami zasobów z interfejsu Grafana UI a narzędziem wdrożeniowym Grafana CLI (`gcx` 1.7). Skrypt traktuje strukturę katalogów w folderze `data/` jako jedyne źródło prawdy o hierarchii folderów w Grafanie (do maksymalnie 4 poziomów zagnieżdżenia) i przekształca wrzucone przez użytkowników eksporty JSON/YAML w deterministyczne, niezależne od środowiska manifesty Kubernetes-style (`folder.yaml`, `d-<slug>.yaml`, `a-<slug>.yaml`).

Identyfikatory zasobów (`metadata.name`) są wyliczane deterministycznie jako skrót SHA-256 ze ścieżki katalogu i tytułu zasobu (`f-<20_hex>`, `d-<20_hex>`, `a-<20_hex>`), co gwarantuje spójność identyfikatorów pomiędzy środowiskami `dev` / `tech` / `prd`. W celu minimalizacji ryzyka (blast radius), narzędzie analizuje historię zmian Git (`git diff` oraz `git show`) i przekazuje do `gcx` wyłącznie deltę zmian (zasoby dodane, zmodyfikowane lub usunięte w danym PR/commicie), zachowując bezpieczną kolejność hierarchiczną:
1. Tworzenie/aktualizacja folderów poziomami od góry do dołu (`L1 -> L2 -> L3 -> L4`),
2. Tworzenie/aktualizacja dashboardów (`d-*.yaml`) oraz reguł alertowych (`a-*.yaml`),
3. Usuwanie skasowanych dashboardów i reguł alertowych,
4. Usuwanie skasowanych pustych folderów od dołu do góry (`L4 -> L3 -> L2 -> L1`).

---

## 2. Opis modułów (`normalizer/`)
- `models.py` – definicje struktur danych (`ParsedFile`, `ValidationIssue`, `PlanItem`, `ExecutionPlan`, `ReconcileResult`) oraz typów wyliczeniowych (`FileKind`, `IssueSeverity`, `PlanActionType`).
- `paths.py` – kanonizacja ścieżek względem `data/` (Unicode NFC), kontrola głębokości drzewa folderów, generowanie skrótów UID (`f-`, `d-`, `a-`) oraz nazw plików z prefiksami typu (`d-<slug>.yaml`, `a-<slug>.yaml`).
- `parser.py` – rozpoznawanie typów plików (odróżnianie surowych eksportów Dashboard/AlertRule z UI od gotowych manifestów kanonicznych GCX oraz ignorowanie plików niemaniestowych np. `.md`).
- `transformer.py` – konwersja surowych struktur JSON/YAML do schematów API Grafany (`folder.grafana.app/v1beta1`, `dashboard.grafana.app/v2` i `v1beta1`, `rules.alerting.grafana.app/v0alpha1`) wraz z czyszczeniem metadanych instancji źródłowej.
- `reconciler.py` – operacje na drzewie katalogów `data/`: wykrywanie kolizji nazw i przekroczeń limitu 4 poziomów, zapis znormalizowanych plików YAML, usuwanie surowych eksportów oraz automatyczne tworzenie i czyszczenie pustych plików `folder.yaml` (algorytm Bottom-Up).
- `planner.py` – wyliczanie delty zmian z `git diff`, odczytywanie metadanych usuniętych plików z historii Gita (`git show`), ochrona przed konfliktem `CREATE+DESTROY` tego samego UID, generowanie raportu `Deploy grafana-observability (TF-style)` oraz budowanie komend `gcx resources push/delete`.
- `cli.py` / `__main__.py` – interfejs wiersza poleceń (`python -m normalizer <mode>`) obsługujący trzy tryby pracy w pipeline CI/CD.

---

## 3. Tryby uruchomienia i parametry CLI

Wywołanie:
```bash
python -m normalizer <mode> [opcje]
```

### Tryby pozycyjne (`mode`):
- `plan` – normalizuje surowe pliki w katalogu `data/`, weryfikuje reguły walidacji (limit 4 poziomów, brak kolizji nazw), wylicza deltę względem `--base-ref`, uruchamia walidację `gcx resources validate` i generuje raport `Plan: X to add, Y to change, Z to destroy.`. Używany zarówno przy pushu na branch roboczy użytkownika (bez commita zwrotnego), jak i w Pull Requeście (przed bot-commitem).
- `deploy` – uruchamiany po Merge na gałęziach środowiskowych (`dev` / `tech` / `prd`); pomija normalizację, wdraża wyłącznie deltę zmian pomiędzy commitami (`gcx resources push` oraz `gcx resources delete`) i generuje raport `Apply: X added, Y changed, Z destroyed.`.

### Parametry konfiguracyjne:
- `--repo-root <path>` – ścieżka do głównego katalogu repozytorium Git (domyślnie `.`).
- `--data-dir <name>` – podkatalog z zasobami Grafany wewnątrz repozytorium (domyślnie `data`).
- `--base-ref <git_ref>` – bazowa referencja Git do wyliczenia delty (domyślnie `HEAD`, w PR np. `origin/prd`, po merge np. `HEAD~1` lub `${{ github.event.before }}`).
- `--target-ref <git_ref>` – docelowa referencja Git używana w trybie `deploy` (domyślnie `HEAD` lub `${{ github.sha }}`).
- `--gcx-bin <path>` – ścieżka do pliku wykonywalnego `gcx` (domyślnie `./gcx`).
- `--context <name>` – nazwa kontekstu środowiska w konfiguracji GCX (np. `dev`, `tech`, `prd`).
- `--summary-file <path>` – zapisuje wygenerowany plan Markdown do wskazanego pliku (przydatne do publikacji komentarza w PR).
- `--skip-gcx` – wylicza i wyświetla plan zmian oraz błędy walidacji bez wywoływania binarki `gcx`.

---

## 4. Kody wyjścia (Exit Codes)
- `0` – wykonanie zakończone sukcesem (brak błędów walidacji `ERROR` oraz poprawne wykonanie wszystkich komend `gcx`). Ostrzeżenia `WARNING` (np. zignorowanie plików powyżej 4. poziomu zagnieżdżenia) są raportowane w podsumowaniu, lecz nie przerywają pracy skryptu (kod `0`).
- `1` – wykryto błędy walidacji polityk (`ERROR`, np. kolizja nazw zasobów w tym samym folderze, uszkodzony plik JSON/YAML, brak tytułu zasobu) — wykonanie `gcx` zostaje zablokowane.
- `!= 0` (kod zwrócony przez proces potomny `gcx`, np. `4`) – błąd wykonania operacji `push` lub `delete` po stronie `gcx` / Grafana API.
- `2` – błąd składni argumentów wywołania CLI (`argparse`).
