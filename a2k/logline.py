"""One JSON object per stdout line, whole -- the shape CloudWatch's
`{ $.event = ... }` filters need.

`print(...)` writes the text and the newline as two separate writes, so two
threads logging at once (admissions in the background, retrievals on request
threads) can glue two objects onto one line, which then parses as neither:
in the full shadow run of 2026-10-06, 3 admissions that did happen (two were
later served) had no cache.admission line. One write per line, under a lock.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

_lock = threading.Lock()


def emit(record: dict[str, Any], *, ensure_ascii: bool = False) -> None:
    line = json.dumps(record, ensure_ascii=ensure_ascii, default=str) + "\n"
    with _lock:
        sys.stdout.write(line)
        sys.stdout.flush()
