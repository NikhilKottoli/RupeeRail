# RupeeRail API (Python)

FastAPI + uvicorn backend for `ScanPayVault` (`contracts/src/ScanPayVault.sol`) on Arc testnet (USDC) and
Whitechain Sepolia (USDT). It quotes UPI payments, co-signs them as the quote signer, relays them gaslessly as the
paymaster, watches the vault, pays the merchant in INR through a black-box payout command exactly once, and
settles, refunds and releases onchain. The HTTP surface follows API contract v1
(`HANDOFF-payment-cycle-api-metamask.md` sections 3.1-3.8). See `HANDOFF.md` for the architecture.

## Run

```bash
cd services/api-py
uv sync                                                   # Python 3.12 venv from uv.lock
uv run uvicorn app.main:app --port 8788                   # API + all background workers
WORKERS=0 uv run uvicorn app.main:app --port 8788         # API only (FX poller still runs)
uv run python -m app.main --port 8788 --no-workers        # same, via the CLI
uv run python -m app.main --port 8788 --dry-run           # workers on, simulate txs but never broadcast
```

Keys are read inside Python only (see `app/keys.py`): each role's `*_PRIVATE_KEY` env var, else `SEED_PHRASE` at
`m/44'/60'/0'/0/0`, from the environment or the repo-root `.env` parsed as text. Only addresses are logged.

```bash
uv run pytest                      # everything (123 tests; 16 are live read-only)
uv run pytest -m "not live"        # offline only
uv run pytest -m live              # eth_call / eth_getLogs against both testnets, never a transaction
uv run python scripts/gen_abi.py   # regenerate app/abi/ScanPayVault.json with forge inspect
```

## Endpoints (v1)

All integers in bodies are decimal strings. Errors are `{"error": "<CODE>", "message": "..."}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `ok`, `chains`, `paymaster`, `settler`, `quoteSigner`, `fx` (+ `diagnostics`) |
| GET | `/config` | per chain: chainId, name, rpc, explorer, vault, token, `vaultDomain`, `permitDomain`, `types`; `paymaster`; `onramp: {rail, testMode}`, which decides whether the web app offers Buy |
| GET | `/account/{chain}/{address}` | token balance, permit nonce, vault allowance (helper) |
| POST | `/quote` | `{chain, user, inr, qrText}` -> `{id, intent, quoteSig, payee, display, signing:{permit, intent}, viewToken}` |
| POST | `/offramp` | `{chain, intent, userSig, permit:{deadline,v,r,s}}` -> `{id, txHash, explorer, paidBy, status:"SUBMITTED"}` |
| GET | `/offramp/{id}` | status, payout, payee (only with `viewToken`), amount, timers (`releasableAt`, `disputeDeadline`), `canDispute`, txs; `?refresh=true` adds live `onchain` |
| GET | `/offramps?user=&chain=` | list of the same views |
| POST | `/dispute` | `{chain, id, deadline, userSig}` -> `{id, txHash, status:"DISPUTED"}` (relays `disputeOfframpWithSig`) |
| GET | `/events?chain=&id=` | array of `{chain, name, id, block, txHash, explorer, args}`, newest first |
| GET | `/events/stream` | SSE; `event:` = contract event name, `data:` = the item above; `ping` every 15 s |
| POST | `/onramp` | `{chain, wallet, inr}` -> `{orderId, amount, symbol, rate, payTo, status:"AWAITING_FIAT"}` |
| GET | `/onramp/{orderId}` | the order (`AWAITING_FIAT` / `PAYING` / `PAID` / `FAILED`, `paidTx`) |
| POST | `/admin/onramp/{orderId}/fiat-received` | header `x-admin-token`, body `{bankRef}` -> settler `onramp(...)` |
| POST | `/admin/offramp/{id}/reverse` | header `x-admin-token` -> settler `reverseOfframp` |
| POST | `/admin/offramp/{id}/refund` | header `x-admin-token`, body `{reason: 1-4}` -> settler `refundOfframp` |

Status enum: `QUOTED SUBMITTED LOCKED COMPLETED DISPUTED RELEASED REFUNDED REVERSED`.
Payout enum: `NONE DISPATCHING DISPATCHED FAILED NEEDS_OPERATOR SKIPPED`.

## Background workers (when `WORKERS` is on)

| Worker | Every | Does |
|---|---|---|
| FX poller | 15 s | CoinDCX `USDCINR` / `USDTINR` bid/ask; quotes refused when older than 2 min. CoinGecko reference (optional). |
| Listener (per chain) | 3 s | `eth_getLogs` from a saved cursor in <= 2,000-block ranges; matches `OfframpLocked` to the stored quote; mirrors all events. |
| Payout runner | on lock | exactly-once hand-off to `PAYOUT_COMMAND` (JSON on stdin, exit 0 = paid); boot recovery flags DISPATCHING rows NEEDS_OPERATOR. |
| Settler | 3 s | retries pending refunds; completes dispatched payments once `completableAt` is reached; ALERT after 1 h. |
| Keepers | 30 s | `reclaimOfframp`, `releaseOfframp`, `claimDisputeTimeout` when due (paymaster key). |

## Environment variables

Names only (see `.env.example`). Defaults in brackets.

- Keys: `SEED_PHRASE`, `QUOTE_SIGNER_PRIVATE_KEY`, `PAYMASTER_PRIVATE_KEY`, `SETTLER_PRIVATE_KEY`, `RUPEERAIL_ENV_FILE` [repo-root `.env`]; `OTHER_WALLET_PRIVATE_KEY` (e2e client only)
- Server: `PORT` [8788], `HOST` [127.0.0.1, CLI only], `DB_PATH` [`data/rupeerail.sqlite`], `CHAINS` [arc,whitechain], `WORKERS` [1], `FX` [1], `DRY_RUN` [0], `LOG_LEVEL` [INFO], `ADMIN_TOKEN` [unset = admin routes disabled], `VIEW_TOKEN_SECRET` [unset = random viewTokens; set = HMAC of the offramp id, shared with `apps/upi-payer`], `EXPOSE_PAYEE_BY_ID` [0], `RATE_LIMIT_PER_MIN` [30]
- Chains: `ARC_RPC_URL`, `WHITECHAIN_RPC_URL`, `ARC_START_BLOCK`, `WHITECHAIN_START_BLOCK` [deploy block], `ARC_MIN_MAX_FEE_GWEI` [20], `WHITECHAIN_MIN_MAX_FEE_GWEI` [0]
- Pricing: `SPREAD_BPS` [80], `QUOTE_TTL_SEC` [90], `MIN_INR_PAISE` [100], `MAX_INR_PAISE` [100000], `DAILY_USER_INR_PAISE` [500000], `FX_POLL_SEC` [15], `FX_MAX_AGE_SEC` [120], `GLOBAL_REF_POLL_SEC` [60]
- Workers: `LISTENER_POLL_SEC` [3], `LISTENER_MAX_RANGE` [2000, capped], `LISTENER_CONFIRMATIONS` [1], `SETTLER_TICK_SEC` [3], `KEEPER_TICK_SEC` [30], `PAYOUT_COMMAND` [`scripts/dev_payout_stub.py`], `PAYOUT_TIMEOUT_SEC` [30], `PAYOUT_MAX_LOCK_AGE_SEC` [600], `COMPLETE_ALERT_SEC` [3600], `TX_RECEIPT_TIMEOUT_SEC` [120], `COLLECT_UPI_ID` [rupeerail@okaxis]

## Scripts

- `scripts/dev_payout_stub.py`: the payout black box for dev (prints "pay ₹X to upiId", exit 0; `STUB_FAIL=1` exits 1).
- `scripts/e2e_client.py`: acts as the frontend against a running server. **It sends real testnet transactions
  through the paymaster**; run it only with approval. `uv run python scripts/e2e_client.py --chain arc --inr 10 [--dispute]`.
- `scripts/gen_abi.py`: regenerates the ABI from `contracts/` with `forge inspect`.
