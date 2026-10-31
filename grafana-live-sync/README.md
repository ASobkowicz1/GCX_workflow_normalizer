# Live Grafana Sync (State Reconciliation & Drift Auditor)

Narzędzie do porównywania stanu repozytorium (`grafana/`) bezpośrednio ze stanem rzeczywistym pobranym z chmury Grafana Cloud (`gcx resources pull`).

Służy jako **Audytor Driftu (Scheduled Cron / Audyt na żądanie)** do weryfikacji, czy zasoby w Grafanie nie zostały zmodyfikowane w UI lub przypadkowo usunięte.

---

## 🎯 Jak działa narzędzie?

1. **Pobranie stanu z Grafana Cloud:**
   - Moduł `normalizer/grafana_state.py` łączy się ze stackiem Grafany (odczytując poświadczenia z `~/.config/gcx/config.yaml`) i pobiera listę zasobów `Folder`, `Dashboard` oraz `AlertRule`.
   - **100% ochrona zasobów ręcznych:** Filtr `is_managed_uid()` analizuje wyłącznie zasoby zarządzane przez repozytorium (posiadające deterministyczne prefiksy `f-`, `d-`, `a-`). Wszystkie zasoby utworzone ręcznie w UI mają inne identyfikatory i **nigdy nie zostaną zmodyfikowane ani usunięte**.

2. **Porównanie 1:1 (Repozytorium kontra żywa Grafana):**
   - **Jest w repo, brak w Grafanie $\rightarrow$ `+ CREATE`:** Wykrywa brakujące zasoby i planuje ich odtworzenie.
   - **Jest w repo i w Grafanie, ale różni się `spec` $\rightarrow$ `~ UPDATE`:** Wykrywa ręczne modyfikacje (*configuration drift*) i planuje przywrócenie stanu z Gita.
   - **Jest identyczny $\rightarrow$ Pomijane:** Zero zbędnych zapytań do API.
   - **Brak w repo, istnieje w Grafanie (z UID `f-/d-/a-`) $\rightarrow$ `- DESTROY`:** Planuje bezpieczne usunięcie (od L4 w górę do L1).

---

## ⚙️ Działanie w GitHub Actions (Narzędzie Administracyjne)

Narzędzie służy wyłącznie administratorom i działa wyłącznie na oficjalnych gałęziach środowiskowych (`prd`, `dev`, `tech`):
1. **Całkowita izolacja od branchy użytkowników:** Workflow nie posiada triggerów `push` ani `pull_request` (żaden commit developera go nie odpali). Próba ręcznego wywołania (`workflow_dispatch`) na gałęzi roboczej usera zostanie natychmiast zablokowana błędem.
2. **Nocny cron (`schedule` o 02:00 UTC):** Wyzwalany automatycznie na gałęzi domyślnej (`main`). Tworzy równoległą macierz dla **wszystkich 3 środowisk (`dev`, `tech`, `prd`)**. Każde zadanie pobiera kod bezpośrednio z oficjalnego brancha i naprawia drift na przypisanym stacku Grafany.
3. **Uruchomienie ręczne przez admina (`workflow_dispatch`):** W formularzu admin wybiera docelowy stack (`all` — domyślnie wszystkie 3, lub konkretny: `prd`, `dev`, `tech`) oraz tryb (`deploy` lub `plan`). Zawsze pobierany jest kod z oficjalnej gałęzi wybranego środowiska.

---

## 🚀 Użycie (Jednoliniowe komendy)

### 1. Tryb audytu (Plan / Dry-Run):
Wykrywa różnice i wyświetla raport Terraform-style bez wprowadzania jakichkolwiek zmian:
```bash
python3 grafana-live-sync/normalizer/cli.py plan --repo-root . --data-dir grafana --gcx-bin gcx --summary-file plan.md
```

### 2. Tryb uzgodnienia (Deploy / Apply):
Wymusza stan z repozytorium na klastrze Grafany (naprawia wykryty drift):
```bash
python3 grafana-live-sync/normalizer/cli.py deploy --repo-root . --data-dir grafana --gcx-bin gcx --summary-file plan.md
```
