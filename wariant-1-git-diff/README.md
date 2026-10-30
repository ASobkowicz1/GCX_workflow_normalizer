# Wariant 1: Git Diff + Bezpieczniki (`--full-sync` & odporność na `404 Not Found`)

## Jak działa ten wariant?
To rozwinięcie bazowego podejścia opartego wyłącznie o historię zmian w Git (`git diff` i `git show`), wzbogacone o dwa bezpieczniki produkcyjne:

1. **Ręczna pełna synchronizacja (`workflow_dispatch` + `--full-sync`):**
   - Jeśli wrzucisz do repozytorium 50 dashboardów przy pierwszym uruchomieniu albo środowisko w Grafanie rozjedzie się z Gitem, możesz w zakładce **Actions** kliknąć **Run workflow**, wybrać środowisko (`dev` / `tech` / `prd`) i zaznaczyć `full_sync: true`.
   - Flaga `--full-sync` porównuje katalog `data/` z pustym drzewem Git (`4b825dc642cb6eb9a060e54bf8d69288fbee4904`), dzięki czemu wypycha wszystkie foldery (L1 -> L4), dashboardy i alerty od zera.
2. **Bezpieczne usuwanie (`404 Not Found` ignore):**
   - Jeśli ktoś usunął wcześniej dashboard ręcznie w UI Grafany, a następnie usuwasz plik w Git, polecenie `gcx resources delete` mogłoby zwrócić błąd `404 Not Found`. W tym wariancie `cli.py` przechwytuje `404 / not found` podczas operacji `delete`, wypisuje ostrzeżenie i nie przerywa wdrożenia.

## Zalety i wady
- **Zalety:** Najszybszy czas działania, zero dodatkowych zapytań HTTP z Pythona do API Grafany przed uruchomieniem `gcx`.
- **Wady:** Podczas zwykłego PR-a nie sprawdza, czy ktoś w międzyczasie nie zmodyfikował lub nie skasował innych zasobów ręcznie w UI Grafany (tzw. *configuration drift*).
