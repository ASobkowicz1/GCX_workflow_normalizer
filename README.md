# Grafana Observability as Code (GCX GitOps Workflow)

Produkcyjne środowisko GitOps do zarządzania zasobami Grafany (Dashboardy, Foldery wielopoziomowe L1–L4, Alert Rules) przy użyciu oficjalnego narzędzia **Grafana CLI (`gcx`)**, silnika normalizacji w Pythonie (`normalizer`) oraz **GitHub Actions**.

---

## 🎯 Architektura produkcyjna ("Skała")

W środowisku produkcyjnym, w którym foldery zarządzane z repozytorium są chronione przed edycją w UI (brak roli Editor dla użytkowników) oraz wszystkie zmiany przechodzą przez Pull Requesty:

1. **Główny silnik wdrożeniowy (`normalizer/`):**
   - Oparty na precyzyjnym **Git Diff** (wdraża wyłącznie zasoby dotknięte w danym PR lub commicie).
   - **Minimalny blast radius:** zmiana jednego alertu dotyka tylko tego jednego alertu i trwa 2–3 sekundy.
   - **Odporność na błędy (404 Not Found ignore):** jeśli zasób został wcześniej usunięty lub nie istnieje, polecenie `delete` nie przerywa wdrożenia.
   - **Bezpiecznik `--full-sync`:** możliwość jednorazowego wypchnięcia całego repozytorium (np. z poziomu zakładki *Actions* -> *Run workflow* dla nowego stacka).

2. **Czyste i bezobsługowe flow dla programisty (PR & Squash Merge):**
   - Programista wrzuca surowy plik JSON lub YAML i tworzy PR. Nie instaluje lokalnie Pythona ani `gcx`.
   - GitHub Actions w **jednym przebiegu** porządkuje pliki, tworzy commit bota (`chore(normalizer): normalize ...`), nadaje mu status `success` przez GitHub API (brak blokady branch protection i brak drugiego przebiegu CI) oraz wkleja plan zmian do komentarza PR.
   - Scalenie do brancha docelowego (`prd`, `dev`, `tech`) odbywa się wyłącznie metodą **Squash and merge**, dzięki czemu do historii produkcyjnej trafia dokładnie jeden czysty commit.

3. **Nocny Audytor Driftu (`wariant-3-grafana-state/`):**
   - Wariant porównujący stan repozytorium 1:1 z żywym stanem w Grafana Cloud (`gcx resources pull`).
   - Służy jako zadanie harmonogramowane (cron job w nocy lub audyt na żądanie) do raportowania, czy stan w chmurze nie rozjechał się z repozytorium Git.

---

## 📂 Struktura katalogów

```text
├── .github/
│   └── workflows/
│       └── grafana-observability.yml   # Główny pipeline GitHub Actions (Plan, Bot-Commit, Deploy, Full-Sync)
├── grafana/                            # Katalog z zasobami Grafany (L1 Foldery -> Podfoldery -> Zasoby)
│   ├── PRD - Applications/
│   │   ├── folder.yaml
│   │   ├── d-series-active.yaml
│   │   └── a-error-rate.yaml
│   └── PRD - Infrastructure/
│       ├── folder.yaml
│       └── Usage/
│           ├── folder.yaml
│           └── d-active-users.yaml
├── normalizer/                         # Główny pakiet produkcyjny GitOps
│   ├── cli.py                          # Punkt wejścia (tryby: plan, deploy, flaga --full-sync)
│   ├── planner.py                      # Budowanie delty Git, kolejkowanie komend GCX i raport Terraform-style
│   ├── reconciler.py                   # Silnik normalizacji plików, deterministycznych UID i drzewa folderów
│   ├── parser.py                       # Parsowanie manifestów Kubernetes oraz dashboardów JSON
│   ├── paths.py                        # Walidacja ścieżek i głębokości folderów (maks. 4 poziomy)
│   └── models.py                       # Dataklasy i typy
├── tests/                              # Zestaw testów jednostkowych (pytest)
├── wariant-1-git-diff/                 # Archiwalna wersja referencyjna wariantu 1
├── wariant-2-hybrid/                   # Wariant hybrydowy (Git Diff + sprawdzanie API)
└── wariant-3-grafana-state/            # Wariant porównania 1:1 z żywą Grafaną (Nocny Audytor Driftu)
```

---

## 🚀 Użycie lokalne

### 1. Wymagania
- Python $\ge$ 3.10
- Zainstalowane zależności: `pip install pyyaml pytest`
- Binarka `gcx` (opcjonalnie, jeśli chcesz uruchomić walidację serwerową)

### 2. Tryb Plan (normalizacja + walidacja + podsumowanie zmian)
```bash
python3 -m normalizer plan --repo-root . --data-dir grafana --base-ref origin/prd --gcx-bin gcx
```

### 3. Tryb Deploy (wdrożenie delty po scaleniu)
```bash
python3 -m normalizer deploy --repo-root . --data-dir grafana --base-ref HEAD~1 --target-ref HEAD --gcx-bin gcx
```

### 4. Wymuszenie pełnej synchronizacji (`--full-sync`)
```bash
python3 -m normalizer deploy --repo-root . --data-dir grafana --full-sync --gcx-bin gcx
```

---

## ⚙️ Konfiguracja GitHub Actions

W ustawieniach repozytorium GitHub (**Settings $\rightarrow$ General $\rightarrow$ Pull Requests**):
1. **Odznacz:** `Allow merge commits` oraz `Allow rebase merging`.
2. **Zaznacz:** `Allow squash merging`.
3. **Ustaw:** *Default commit message* $\rightarrow$ **Pull request title**.
4. W sekcji **Branches**: ustaw dla `prd`, `dev`, `tech` regułę ochrony gałęzi wymagającą status checka `Observability deploy`.
