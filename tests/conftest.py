from __future__ import annotations

import sys
from pathlib import Path

# Ensure tests can import top-level modules like config.py and state.py.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
