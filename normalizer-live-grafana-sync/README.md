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

## 🚀 Użycie (Jednoliniowe komendy)

### 1. Tryb audytu (Plan / Dry-Run):
Wykrywa różnice i wyświetla raport Terraform-style bez wprowadzania jakichkolwiek zmian:
```bash
python3 normalizer-live-grafana-sync/normalizer/cli.py plan --repo-root . --data-dir grafana --gcx-bin gcx
```

### 2. Tryb uzgodnienia (Deploy / Apply):
Wymusza stan z repozytorium na klastrze Grafany (naprawia wykryty drift):
```bash
python3 normalizer-live-grafana-sync/normalizer/cli.py deploy --repo-root . --data-dir grafana --gcx-bin gcx
```
