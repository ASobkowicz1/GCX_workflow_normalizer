# Wariant 2: Hybrydowy (`git diff` + Weryfikacja zasobów w API Grafany)

## Jak działa ten wariant?
Łączy szybkość `git diff` z wiedzą o tym, co faktycznie znajduje się na docelowym stacku Grafany (`normalizer/grafana_state.py`):

1. **Zakres zmian brany z Gita (`git diff`):**
   - Sprawdzamy tylko pliki dotknięte w bieżącym PR-ze lub commicie.
2. **Weryfikacja stanu tych zasobów w żywej Grafanie (`/apis/...`):**
   - Jeśli `git diff` mówi `M` (zmodyfikowany plik -> `UPDATE`), ale ktoś wcześniej skasował ten dashboard ręcznie w UI Grafany -> plan automatycznie zmienia akcję na **`+ CREATE`**.
   - Jeśli `git diff` mówi `A` (nowy plik -> `CREATE`), ale zasób o tym UID już leży w Grafanie -> plan zmienia akcję na **`~ UPDATE`**.
   - Jeśli `git diff` mówi `D` (usunięty plik -> `DESTROY`), ale zasób już wcześniej został usunięty w UI Grafany -> normalizer **pomija `DESTROY`** (brak błędu `404 Not Found`).
   - **Auto-odbudowa brakujących folderów nadrzędnych:** Jeśli modyfikujesz tylko sam plik dashboardu (`d-*.yaml`), a jego folder nadrzędny (`folder.yaml`) nie istnieje jeszcze na serwerze Grafany (lub został usunięty w UI), normalizer automatycznie dołącza brakujący `folder.yaml` jako `+ CREATE`!
