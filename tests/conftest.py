"""Konfiguracja środowiska testowego pytest dla pakietów normalizer oraz live_sync."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_SYNC_ROOT = REPO_ROOT / "grafana-live-sync"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if str(LIVE_SYNC_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_SYNC_ROOT))
