from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import NXT_LAUNCH_DATE
from src.nxt_client import NxtClient


def main() -> None:
    changes = NxtClient().fetch_changes(NXT_LAUNCH_DATE, date.today())
    composite_keys = [
        (item.change_date, item.stock_code, item.change_type, item.reason)
        for item in changes
    ]
    reasons = Counter((item.change_type, item.reason) for item in changes)
    profile = {
        "grain": "date x stock_code x change_type x reason",
        "rows": len(changes),
        "unique_composite_keys": len(set(composite_keys)),
        "duplicate_rows": len(composite_keys) - len(set(composite_keys)),
        "first_date": min((item.change_date for item in changes), default=None),
        "last_date": max((item.change_date for item in changes), default=None),
        "invalid_stock_codes": sum(
            not (len(item.stock_code) == 6 and item.stock_code.isdigit())
            for item in changes
        ),
        "invalid_change_types": sum(
            item.change_type not in {"편입", "편출"} for item in changes
        ),
        "reason_counts": {
            f"{change_type}|{reason}": count
            for (change_type, reason), count in sorted(reasons.items())
        },
    }
    print(json.dumps(profile, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
