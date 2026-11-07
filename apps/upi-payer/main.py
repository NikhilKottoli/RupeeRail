#!/usr/bin/env python3
"""RupeeRail UPI payer.

    main.py run                                 watch for locked offramps and pay them
    main.py report                              tell a hosted agent server how payments ended
    main.py check                               backend, device, foreground app, readable screen
    main.py dump-screen                         print the text uiautomator can read right now
    main.py rehearse --upi-id a@ybl --inr 1     walk the flow, verify the screen, stop before Pay

Only `run` with UPI_LITE_LIVE=1 can tap Pay.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import agent
import payer
import reporter

UNKNOWN_ID = "0x" + "00" * 32


def load_env_file(path: Path) -> None:
    """Fills in anything not already set in the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def make_backend() -> agent.Backend:
    url, secret = os.environ.get("RUPEERAIL_API_URL"), os.environ.get("VIEW_TOKEN_SECRET")
    if not url or not secret:
        raise ValueError("set RUPEERAIL_API_URL and VIEW_TOKEN_SECRET (see .env.example)")
    return agent.Backend(url, secret)


def check_backend(backend: agent.Backend) -> bool:
    """Proves the URL works. Only a real payment can prove the secret, because any id without one just hides the payee."""
    try:
        backend.events(limit=1)
        backend.payment(UNKNOWN_ID)
    except agent.BackendRefused as e:
        if e.code == "NOT_FOUND":
            print("backend:   reachable (VIEW_TOKEN_SECRET is checked at the first payment)")
            return True
        print(f"backend:   REFUSED ({e})")
    except Exception as e:
        print(f"backend:   NOT REACHABLE ({e})")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("report")
    sub.add_parser("check")
    sub.add_parser("dump-screen")
    rehearse = sub.add_parser("rehearse")
    rehearse.add_argument("--upi-id", required=True)
    rehearse.add_argument("--inr", default="1")
    rehearse.add_argument("--payee-name")
    args = ap.parse_args(argv)

    load_env_file(payer.APP_DIR / ".env")
    try:
        cfg = payer.Config.from_env()
        adb = payer.Adb(cfg.adb_binary, cfg.adb_serial)
        if args.command == "run":
            backend = make_backend()
            settings = agent.AgentConfig(
                poll_sec=float(os.environ.get("PAYER_POLL_SEC") or 2),
                max_lock_age_sec=int(os.environ.get("PAYER_MAX_LOCK_AGE_SEC") or 600),
            )
            limits = f"Rs {payer.format_paise(cfg.max_paise)} per payment, Rs {payer.format_paise(cfg.daily_cap_paise)} per 24 h"
            payer.log(f"{'LIVE: real money' if cfg.live else 'dry run'}; {limits}; watching {backend.base_url}")
            agent.Agent(backend, settings, cfg).run()
            return 0
        if args.command == "report":
            reporter.from_env(cfg.db_path).run()
            return 0
        if args.command == "check":
            backend_ok = check_backend(make_backend())
            return 0 if payer.cmd_check(cfg, adb) == 0 and backend_ok else 1
        if args.command == "dump-screen":
            return payer.cmd_dump(cfg, adb)
        return payer.cmd_rehearse(cfg, adb, args.upi_id, args.inr, args.payee_name)
    except KeyboardInterrupt:
        return 130
    except (ValueError, json.JSONDecodeError) as e:
        print(f"bad config: {e}", file=sys.stderr)
        return 2
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"adb problem: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
