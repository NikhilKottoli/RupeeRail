"""Sends the final result to a webhook. A failed delivery never affects the payment."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger("rupeerail.agent")


def build_payload(session_id: str, goal: str, status: str, offramp_id: str | None, tx_hash: str | None,
                  amount_paise: int | None, payee: str | None) -> dict:
    return {
        "session_id": session_id,
        "status": status,
        "offramp_id": offramp_id,
        "txHash": tx_hash,
        "amount_inr": amount_paise / 100 if amount_paise is not None else None,
        "payee": payee,
        "goal": goal,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def send_webhook(url: str, payload: dict, post=httpx.post) -> bool:
    if not url:
        return False
    try:
        response = post(url, json=payload, timeout=10.0)
        return response.status_code < 400
    except httpx.HTTPError as e:
        log.warning("webhook delivery failed: %s", type(e).__name__)
        return False
