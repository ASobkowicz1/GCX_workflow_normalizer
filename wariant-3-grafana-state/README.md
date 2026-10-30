# GCX Normalizer (Live Grafana State Reconciliation)

## Jak działa normalizer?
Normalizer porównuje katalog zasobów w repozytorium (`grafana/`) bezpośrednio ze stanem rzeczywistym (*Actual State*) pobranym z docelowego stacka Grafany:

1. **Odpytanie API Grafany (`/apis/...`):**
   - Moduł `normalizer/grafana_state.py` łączy się z docelowym stackiem Grafany (odczytując URL, token i `stack-id` z `~/.config/gcx/config.yaml`) i pobiera listę wszystkich zasobów `Folder`, `Dashboard` oraz `AlertRule`.
   - **100% ochrona zasobów spoza repozytorium:** Filtr `is_managed_uid()` bierze pod uwagę wyłącznie zasoby posiadające nasze deterministyczne UID-y (`^(f|d|a)-[0-9a-f]{20}$`). Wszystkie dashboardy, foldery i alerty utworzone ręcznie w UI mają standardowe UID-y Grafany i **nigdy nie zostaną usunięte**.
2. **Porównanie 1:1 (`data/` kontra żywa Grafana):**
   - **Jest w `data/`, nie ma w Grafanie -> `+ CREATE`:** Wykrywa zarówno nowe pliki dodane w PR, jak i dashboardy, które ktoś przypadkiem skasował ręcznie w UI Grafany.
   - **Jest w `data/` i w Grafanie, ale różni się `spec` lub folder -> `~ UPDATE`:** Wykrywa zmiany w plikach YAML oraz ręczne modyfikacje w UI (*configuration drift*) i przywraca stan z Gita.
   - **Jest w `data/` i w Grafanie, `spec` jest identyczny -> Pomijane:** Nie wywołuje dla tych plików `gcx`, dzięki czemu wdrożenie działa szybko.
   - **Nie ma w `data/`, a leży w Grafanie (z UID `f-/d-/a-`) -> `- DESTROY`:** Usuwa zasób z Grafany (zachowując bezpieczną kolejność kasowania folderów od L4 w górę do L1).
