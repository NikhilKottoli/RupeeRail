# Off-ramp backend notes

Findings for the agent plan (`README-agents.md`, section 0), checked against `services/api-py` on 2026-11-06.

## Where the agent server goes

In a new service, `agent-server/`. The backend owns the wallet keys and live settlement, and the plan says not to change its existing endpoints.

## Endpoints

All integers are decimal strings. Errors are `{"error": CODE, "message": "..."}`.

| Method | Path | Auth | Used by the agent |
|---|---|---|---|
| GET | `/health` | None | Preflight |
| GET | `/config` | None | Chains, tokens, domains, limits |
| GET | `/account/{chain}/{address}` | None | Wallet balance |
| POST | `/quote` | None | `{chain, user, inr, qrText}` returns `{id, intent, quoteSig, payee, display, signing{permit,intent}, viewToken}` |
| POST | `/offramp` | None | `{chain, intent, userSig, permit{deadline,v,r,s}}` returns `{id, txHash, explorer, paidBy, status:"SUBMITTED"}` |
| GET | `/offramp/{id}` | Optional `x-view-token` | Status poll. `payee` is null without the token |
| GET | `/offramps?user=&chain=` | None | History |
| POST | `/dispute` | None | `{chain, id, deadline, userSig}` |
| GET | `/events`, `/events/stream` | None | Chain events, as a list or SSE |
| POST | `/onramp`, GET `/onramp/{id}` | None | Not used |
| POST | `/admin/...` | `x-admin-token` | Not used |

Error codes the agent should handle: `AMOUNT_OUT_OF_RANGE`, `BAD_CHAIN`, `BAD_INTENT_SIG`, `BAD_PERMIT_SIG`, `DAILY_LIMIT`, `FX_STALE`, `INSUFFICIENT_BALANCE`, `INTENT_EXPIRED`, `PENDING`, `QUOTE_EXPIRED`, `QUOTE_MISMATCH`, `QUOTE_USED`, `RATE_LIMITED`, `RPC_UNAVAILABLE`, `SEND_FAILED`, `SIMULATION_FAILED`, `UNKNOWN_QUOTE`.

## Chains

Arc is already supported, and nothing needs changing. The `chain` field accepts `arc` or `whitechain`. Each is configured from `deployments/*.json`.

| | Arc testnet |
|---|---|
| `chain` value | `arc` |
| Chain ID | 5042002 (confirmed against docs.arc.io) |
| Token | USDC `0x3600000000000000000000000000000000000000`, 6 decimals |
| Permit domain | name `USDC`, version `2` |
| Vault | `0x878734374e6131FCeEEB83FA8605Ab49f4D21836` |

## Quote expiry

**90 seconds, not the 5 to 10 minutes the plan assumes.** `QUOTE_TTL_SEC` defaults to 90, and the permit deadline is the same as the intent deadline. So the quote must be requested at the last moment, and signing can't wait on anything slow.

## "Section 5"

**Not in this repo.** The plan refers to a backend doc with quote verification rules, probably `HANDOFF-payment-cycle-api-metamask.md`, which the backend cites but which isn't checked in. `backend-handoff.md` section 5 covers privacy instead. Until the file arrives, `verify.py` will use the checks in section 7 of the plan, plus these checks against the signed payloads:

- The domain `chainId` and `verifyingContract` match `/config`.
- `user` and the permit `owner` are the agent's wallet.
- The permit `spender` is the vault, and `value` equals the intent `amount`.
- `inrPaise` equals the QR's `am`, and `qrCommitment` is non-zero.

## Things that change the plan

1. **`COMPLETED` isn't proof of payment.** With the stub payout, it arrives about 15 seconds after the lock, whether or not the phone paid. In live tests it came 12 to 14 seconds before the phone finished. A success callback can't rely on it alone.
2. **`RELEASED` takes 24 hours**, so a demo never reaches it. The poll can't wait for it.
3. **The phone pays a rebuilt QR.** `tr`, `mc` and `sign` are dropped, because `GET /offramp/{id}` doesn't return the raw text. A merchant may not match the payment to the order.
4. **BharatQR is refused.** QRs starting `000201` are rejected in `app/qr.py`.
5. **A live payer pays anything.** With `UPI_LITE_LIVE=1` and no allowlist, any test quote sends real rupees. Agent tests must run with the payer stopped, in dry-run, or allowlisted.
