# Backend handoff: `services/api-py`

Python 3.12, FastAPI + uvicorn, web3.py 7 (`AsyncWeb3`), eth-account, SQLite (stdlib). Contract source of
truth: `contracts/src/ScanPayVault.sol` (commit 5355d67); ABI generated into `app/abi/ScanPayVault.json` by
`scripts/gen_abi.py`. The HTTP API follows contract v1 (`HANDOFF-payment-cycle-api-metamask.md` sections 3.1-3.8).

## 1. Architecture

```
            Frontend / wallet (signs Permit + OfframpIntent + DisputeIntent; never sends a tx)
                 |  HTTP (app/api.py)
   +-------------+--------------------------------------------------------------------------+
   |  quote_service ---- fx (CoinDCX poll) ---- quote_math ---- qr ---- commitment ---- eip712 |
   |      | sign hashIntent (quote signer key, local)        reads token.nonces(user)          |
   |      v                                                                                    |
   |  store (SQLite: quotes[private qrText+salt], payments, onramp_orders, cursors, disputes, |
   |         events)                                                                           |
   |      ^                    ^                     ^                    ^                     |
   |  paymaster          listener (per chain)   payout_runner        settler / keepers          |
   +------|--------------------|---------------------|--------------------|--------------------+
          | chain.py: retried reads, per-sender lock + nonce queue, simulate (eth_call) then send,
          | EIP-1559 fees with Arc's 20 gwei maxFeePerGas floor, custom-error decoding
          v
   ScanPayVault (Arc 0x8787...1836, Whitechain 0x326f...65c1)
```

| Module | Contract function(s) | Key used |
|---|---|---|
| `quote_service` | none (signs the `hashIntent` digest offchain; reads `token.nonces`) | quote signer |
| `paymaster` | `offramp(intent, userSig, quoteSig, permit)` | paymaster |
| `dispute_relay` | `disputeOfframpWithSig(id, deadline, userSig)` | paymaster |
| `settler` | `completeOfframp`, `refundOfframp`, `reverseOfframp`, `onramp` | settler |
| `keepers` | `reclaimOfframp`, `releaseOfframp`, `claimDisputeTimeout` | paymaster (permissionless) |
| `listener` | reads `eth_getLogs` (all vault events), `deadlines`, `offramps` | none |
| `chain` / tests | `hashIntent`, `hashDispute`, `eip712Domain`, `quoteSigner`, `settler`, token `DOMAIN_SEPARATOR` | none |

Request path for one payment:
1. `POST /quote` validates the QR first, then creates a random `id` and `salt`, sets
   `qrCommitment = keccak256(abi.encode(salt, qrText))`, computes the rate and amount, enforces the limits,
   signs `hashIntent` and stores the row. It returns `intent`, `quoteSig` and the two ready-to-sign EIP-712
   payloads.
2. `POST /offramp` checks, in order:
   - the quote exists, every field matches, and it is unused and unexpired;
   - the user signature (EOA ecrecover, or ERC-1271 if the user has code);
   - the permit against `token.nonces` (a bad permit is accepted only if the allowance already covers it);
   - the balance.

   It then claims the quote atomically, simulates, and sends. It returns `SUBMITTED`.
3. The listener sees `OfframpLocked`. It matches `id, user, amount, inrPaise, rateE6, qrCommitment` against
   the stored quote **and** recomputes `keccak(abi.encode(salt, qrText))`. On a mismatch or an unknown id it
   calls settler refund with reason 4. It re-parses the QR: a bad QR gets reason 1, and an `am` that differs
   from the signed amount gets reason 2. Otherwise it hands the payment to the payout runner. It never pays
   on the event alone.
4. The payout runner (exactly once, see section 4) leads to DISPATCHED. The settler completes once chain time
   is at or past `completableAt`. The keeper releases after the 24 h window, or the user disputes through
   `/dispute`, and the arbiter or the timeout keeper finishes it.

## 2. Data model (`app/store.py`)

- **quotes**, the private source of truth:
  - `id` (PK), `chain`, `user`, `amount`, `inr_paise`, `rate_e6`;
  - **`qr_text`, `salt`**, `qr_commitment`, `deadline`, `quote_sig`;
  - `upi_id`, `payee_name`, `market`, `bid`, `view_token`;
  - `status` (`QUOTED`, `SUBMITTED`, `USED`), `created_at`, `used_at`, `offramp_tx`, `last_error`.

  Back this table up: without `salt` and `qr_text` you cannot prove which payee a payment was for.
- **payments**, the onchain mirror plus payout state:
  - identity: `id`, `chain`, `user`, `amount`, `inr_paise`, `rate_e6`, `qr_commitment`;
  - onchain mirror: `status` (`NONE`, `LOCKED`, `COMPLETED`, `REFUNDED`, `REVERSED`, `RELEASED`, `DISPUTED`), `refund_reason`;
  - payout: `dispatch_state` (`NONE`, `DISPATCHING`, `DISPATCHED`, `FAILED`, `NEEDS_OPERATOR`, `SKIPPED`), `dispatch_detail`, `dispatch_started_at`, `dispatched_at`, `pending_refund`;
  - quote match: `match_ok`, `match_detail`;
  - `payout_ref`, plus the `lock_tx`, `lock_block` and `locked_at` of the lock;
  - timers:
    - `completable_at`, `reclaimable_at`, `dispute_window`;
    - `releasable_at`: the dispute-window end, computed as completedAt + disputeWindow;
    - **`dispute_deadline`**: the arbiter deadline, kept in its own column;
  - `txs` (JSON label to hash), `alerted`.
- **onramp_orders**:
  - `id` (the orderId), `chain`, `wallet`, `inr_paise`, `amount`, `rate_e6`, `market`, `ask`;
  - `status` (`AWAITING_FIAT`, `PAYING`, `PAID`, `FAILED`), `bank_ref`, `tx`, `last_error`.
- **cursors** `(chain, block)`, **disputes** `(id, chain, user, deadline, user_sig, status, tx, error)`, and
  **events** `(chain, tx, log_index, block, name, ref_id, args)`, deduplicated by the primary key.

## 3. State machine mirror

```
quote:   QUOTED --claim--> SUBMITTED --receipt--> USED    (simulation revert -> back to QUOTED; listener lock -> USED)
public:  QUOTED -> SUBMITTED -> LOCKED -> COMPLETED -> RELEASED
                                   |          |-> DISPUTED -> REVERSED | RELEASED
                                   |          '-> REVERSED
                                   '-> REFUNDED (1-4 settler, 5 reclaim)
```

- The onchain `deadlines(id).releasableAt` changes meaning once the offramp is DISPUTED: it then holds the
  arbiter deadline. The backend never stores the overloaded value as `releasable_at`:
  - `OfframpCompleted` computes `releasable_at = blockTimestamp + disputeWindow`;
  - `OfframpDisputed` reads the slot into `dispute_deadline`.
- The API exposes `timers.releasableAt` (the window end only) and `timers.disputeDeadline` (only while DISPUTED).
- Keepers read the onchain status first, then interpret the slot.

## 4. Exactly-once and idempotency guarantees

- **Payout is exactly once.**
  - `cas_dispatch(NONE -> DISPATCHING)` is committed **before** the black box is spawned; concurrent
    dispatches lose the compare-and-swap and return `skipped`.
  - At boot, any row still DISPATCHING becomes NEEDS_OPERATOR and is never retried automatically.
  - The offramp id is passed as `idempotencyKey`.
  - The payout starts only if the lock is at most 10 minutes old; otherwise the backend refunds with reason 3.
- **Complete, refund, reverse and keeper actions are idempotent.** Each reads `offramps(id).status` (and the
  deadlines) first; a second call finds the state changed and does nothing.
  - `completeOfframp` uses payoutRef = `keccak256(id)`, or `keccak256(utr)` if the payout command prints
    `{"utr": ...}`. It is unique and non-zero.
  - A failed refund stays in `pending_refund` and the settler loop retries it.
- **Quotes are single-use.** `claim_quote(QUOTED -> SUBMITTED)` is a compare-and-swap, so only one
  `/offramp` call per quote can reach the send. Onchain, `OfframpIdUsed` is the backstop.
- **Onramp is once per order.** The order moves AWAITING_FIAT -> PAYING by compare-and-swap, and `onrampDone(orderId)` is
  checked onchain before sending; the vault also rejects a repeat with `OnrampAlreadyPaid`.
- **Disputes are relayed once.** The `disputes` row is claimed before sending, and the contract makes the
  COMPLETED -> DISPUTED transition happen only once.
- **No nonce races.** Sends are serialised per (chain, sender) with an asyncio lock, and the nonce is
  max(pending count, local next).

## 5. Privacy

- Offramp ids are public in `OfframpLocked`. `GET /offramp/{id}` therefore returns `payee: null` unless the
  caller passes the `viewToken` from `/quote` (as the `?viewToken=` query parameter or the `x-view-token`
  header).
- `EXPOSE_PAYEE_BY_ID=1` restores the literal v1 behaviour, which leaks merchant UPI IDs to anyone who reads the chain.
- `salt` is never returned by any route. `qrText` is returned by `GET /offramp/{id}` only to a viewToken holder, so a merchant's `tr`, `mc` and `sign` fields reach the phone.
- With `VIEW_TOKEN_SECRET` set, each viewToken is `HMAC-SHA256(secret, id)`, so `apps/upi-payer` can compute it from
  the id in `OfframpLocked` and read the payee. A leaked secret exposes every payee quoted while it was set.

## 6. NOT done yet

- **A real UPI payout provider** (for example RazorpayX). The provider must be queried by `idempotencyKey`
  before refunding on a **timeout**. Today a timeout is treated as a failure and refunded with reason 3, which
  could double-pay if the provider actually paid.
- **KYC, residency and sanctions checks** in `quote_service`, and authentication for the user-facing
  endpoints. The rate limiter is in-memory and per process.
- **The ₹2,000 pool limit** mentioned in the build spec section 6.1 is not enforced (unclear definition).
  Today's caps are ₹1-₹1,000 per payment and ₹5,000 per user per 24 h, counted from issued quotes.
- **Alerts** are log lines (`ALERT ...`) only. Nothing pages anyone yet for NEEDS_OPERATOR, completion
  windows closing, or a quote-signer mismatch at startup.
- **Hosting and a public URL** (the Arc Studio preview cannot reach localhost), TLS, a process supervisor,
  and DB backups.
- **Key separation (C2).** On testnet all roles are one exposed seed. Set separate `*_PRIVATE_KEY` values
  before a real demo.
- **Multi-instance safety.** SQLite and in-process locks assume a single server process.
- **Reorgs.** One confirmation by default and no reorg rewind. Raise `LISTENER_CONFIRMATIONS` for production.
- **A pool-balance or daily-cap pre-check** before creating onramp orders (the simulation catches it at payout time).

## 7. Operating notes and gotchas

- **First boot replays history.** The listener starts from each vault's deploy block. Every historical
  `OfframpLocked` has no quote in a fresh DB, so the listener asks the settler for a reason-4 refund. The
  settler reads the status first, so only offramps **still LOCKED** are actually refunded. At the time of
  writing none were: all 13 were final. To skip history, set `ARC_START_BLOCK` / `WHITECHAIN_START_BLOCK`.
- **`OTHER_WALLET` is the arbiter.** `OTHER_WALLET_ADDRESS` is `0x9AB6...eEe9`, which is also the arbiter. The
  spec warns against using it as a clean test user.
- **Arc RPC rate limits.** The Arc RPC is public and rate-limited. Reads retry with exponential backoff.
  Sends are never auto-retried.
- **Catch-up time.** The first catch-up takes about 25 s with the current history, because of several RPC reads per event.

## 8. How to run the live e2e (needs user approval: it broadcasts)

```bash
cd services/api-py
ADMIN_TOKEN=... uv run uvicorn app.main:app --port 8788          # workers on, NOT dry-run
# in another shell (user key = OTHER_WALLET_PRIVATE_KEY, loaded in Python, only addresses printed)
uv run python scripts/e2e_client.py --chain arc --inr 10
uv run python scripts/e2e_client.py --chain whitechain --inr 10 --dispute
```

Expected sequence:
1. The quote prints `Pay Rs 10 to Chai Point (chaipoint@okaxis)`.
2. `/offramp` returns `SUBMITTED` with the paymaster's lock tx.
3. The listener matches the lock and the stub prints `pay ₹10.00 to chaipoint@okaxis`.
4. About 15 s later the settler sends `completeOfframp`, and the poll shows `COMPLETED / DISPATCHED`.
5. With `--dispute`, the relayed `disputeOfframpWithSig` brings it to `DISPUTED`, with `timers.disputeDeadline`
   set about 7 days ahead.

The user wallet needs about 0.11 USDC or USDT, and no gas.
