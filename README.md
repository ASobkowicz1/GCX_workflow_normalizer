# Grafana Observability as Code (GCX GitOps Workflow)

Produkcyjne środowisko GitOps do zarządzania zasobami Grafany (Dashboardy, Foldery wielopoziomowe L1–L4, Alert Rules) przy użyciu oficjalnego narzędzia **Grafana CLI (`gcx`)**, silnika normalizacji w Pythonie (`normalizer`) oraz **GitHub Actions**.

---

## 🎯 Architektura produkcyjna ("Skała")

W środowisku produkcyjnym, w którym foldery zarządzane z repozytorium są chronione przed edycją w UI (brak roli Editor dla użytkowników) oraz wszystkie zmiany przechodzą przez Pull Requesty:

1. **Główny silnik wdrożeniowy (`normalizer/`):**
   - Oparty na precyzyjnym **Git Diff** (wdraża wyłącznie zasoby dotknięte w danym PR lub commicie).
   - **Minimalny blast radius:** zmiana jednego alertu dotyka tylko tego jednego alertu i trwa 2–3 sekundy.
   - **Odporność na błędy (404 Not Found ignore):** jeśli zasób został wcześniej usunięty lub nie istnieje, polecenie `delete` nie przerywa wdrożenia.
   - **Bezpiecznik `--full-push`:** możliwość jednorazowego wypchnięcia wszystkich zasobów z repozytorium (np. z poziomu zakładki *Actions* -> *Run workflow* dla nowego stacka lub Disaster Recovery). Nie kasuje zasobów w Grafanie – bezpieczny bootstrap.

2. **Czyste i bezobsługowe flow dla programisty (PR & Squash Merge):**
   - Programista wrzuca surowy plik JSON lub YAML i tworzy PR. Nie instaluje lokalnie Pythona ani `gcx`.
   - GitHub Actions w **jednym przebiegu** porządkuje pliki, tworzy commit bota (`chore(normalizer): normalize ...`), nadaje mu status `success` przez GitHub API (brak blokady branch protection i brak drugiego przebiegu CI) oraz wkleja plan zmian do komentarza PR.
   - Scalenie do brancha docelowego (`prd`, `dev`, `tech`) odbywa się wyłącznie metodą **Squash and merge**, dzięki czemu do historii produkcyjnej trafia dokładnie jeden czysty commit.

---

## 📂 Struktura katalogów

```text
├── .github/
│   └── workflows/
│       └── grafana-observability.yml   # Główny pipeline GitHub Actions (Plan, Bot-Commit, Deploy, Full-Push)
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
│   ├── cli.py                          # Punkt wejścia (tryby: plan, deploy, flaga --full-push)
│   ├── planner.py                      # Budowanie delty Git, kolejkowanie komend GCX i raport Terraform-style
│   ├── reconciler.py                   # Silnik normalizacji plików, deterministycznych UID i drzewa folderów
│   ├── parser.py                       # Parsowanie manifestów Kubernetes oraz dashboardów JSON
│   ├── paths.py                        # Walidacja ścieżek i głębokości folderów (maks. 4 poziomy)
│   └── models.py                       # Dataklasy i typy
├── tests/                              # Zestaw testów jednostkowych (pytest)
└── grafana-live-sync/                  # Osobne narzędzie audytu/synchronizacji ze stanem żywym (własne README.md)
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

### 4. Wymuszenie pełnego wypchnięcia zasobów (`--full-push`)
Wypycha 100% plików z repozytorium do Grafany (bez usuwania czegokolwiek). Przydatne przy onboardingu lub Disaster Recovery:
```bash
python3 -m normalizer deploy --repo-root . --data-dir grafana --full-push --gcx-bin gcx
```

### 5. Testowanie lokalne na katalogu przykładowym (`example_repo`)
Możesz przetestować działanie planera na załączonych danych przykładowych:
```bash
python3 -m normalizer plan --repo-root . --data-dir example_repo/data --skip-gcx
```

### 6. Uruchomienie testów jednostkowych
```bash
pytest -v
```

---

## 🌿 Topologia gałęzi

1. **`main` (Domyślna gałąź repozytorium):**
   - Techniczna baza repozytorium. Nie jest powiązana z żadnym stackiem w Grafana Cloud (brak wdrożeń).
   - Z tej gałęzi GitHub Actions wyzwala nocny cron (`schedule`), który dynamicznie audytuje i naprawia drift na 3 środowiskach (`dev`, `tech`, `prd`).
2. **`prd`, `dev`, `tech` (Główne gałęzie środowiskowe):**
   - Odpowiadają 1:1 trzem stackom w Grafana Cloud.
   - Wszelkie zmiany lądują na nich wyłącznie przez Pull Requesty scalane metodą **Squash and merge**.
3. **Branche użytkowników (Feature branches):**
   - Rozpoczynają się od prefiksu środowiska (np. `dev/nowy-dashboard`, `tech/alert-cpu`, `prd/fix`) i celują w odpowiednią gałąź (`dev`, `tech` lub `prd`).

---

## ⚙️ Konfiguracja GitHub Actions

1. **W ustawieniach repozytorium GitHub (Settings $\rightarrow$ General $\rightarrow$ Pull Requests):**
   - ❌ **Odznacz:** `Allow merge commits` oraz `Allow rebase merging`.
   - ✅ **Zaznacz:** `Allow squash merging`.
   - 🏷️ **Ustaw:** *Default commit message* $\rightarrow$ **Pull request title**.
2. **Ochrona gałęzi (Settings $\rightarrow$ Branches):**
   - Dla gałęzi `prd`, `dev`, `tech` włącz regułę ochrony wymagającą status checka `Observability deploy`.
3. **Uprawnienia workflow (Settings $\rightarrow$ Actions $\rightarrow$ General):**
   - W sekcji *Workflow permissions* upewnij się, że zaznaczona jest opcja **Read and write permissions** (wymagane dla bota i nadawania statusów przez API).

---

## Testy

1. Główny silnik (GitOps PR / merge):
   # Tryb Plan (normalizacja + walidacja + podsumowanie             
   Terraform-style)
   python3 -m normalizer plan --repo-root . --data-dir grafana      
   --base-ref origin/prd --gcx-bin gcx

   # Tryb Deploy (wdrożenie delty po scaleniu)
   python3 -m normalizer deploy --repo-root . --data-dir grafana    
   --base-ref HEAD~1 --target-ref HEAD --gcx-bin gcx

   # Bootstrap / Disaster Recovery (pełny push bez kasowania)       
   python3 -m normalizer deploy --repo-root . --data-dir grafana    
   --full-push --gcx-bin gcx

2. Narzędzie audytu driftu (Live Sync):
   # Audyt driftu (Plan / Dry-Run względem żywej Grafany)           
   python3 grafana-live-sync/live_sync/cli.py plan --repo-root .    
   --data-dir grafana --gcx-bin gcx --summary-file plan.md          

   # Naprawa driftu (Deploy / Reconcile)
   python3 grafana-live-sync/live_sync/cli.py deploy --repo-root .  
   --data-dir grafana --gcx-bin gcx --summary-file plan.md   