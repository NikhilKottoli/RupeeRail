#!/usr/bin/env python3
"""Regenerates app/abi/ScanPayVault.json from contracts/src via `forge inspect` (never hand-written).

Usage: uv run python scripts/gen_abi.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
REPO = SERVICE.parents[1]
OUT = SERVICE / "app" / "abi" / "ScanPayVault.json"


def main() -> int:
    res = subprocess.run(["forge", "inspect", "ScanPayVault", "abi", "--json"], cwd=REPO / "contracts",
                         capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        return res.returncode
    abi = json.loads(res.stdout)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(abi, indent=2) + "\n")
    kinds: dict[str, int] = {}
    for item in abi:
        kinds[item["type"]] = kinds.get(item["type"], 0) + 1
    print(f"wrote {OUT.relative_to(SERVICE)}: {kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
