# RupeeRail

Scan any UPI QR and pay it from a stablecoin wallet with no gas; the merchant receives ordinary INR. Built at ETHGlobal Mumbai, on Arc testnet (USDC) and Whitechain Sepolia (USDT). Current progress and decisions are in `docs/status.md`.

## Rules that can cost money

- **A live payer spends real rupees.** With `UPI_LITE_LIVE=1`, any lock on either vault can make the phone pay. Before any test that submits to `/offramp`, check whether a payer is running, and if so use dry-run or `UPI_LITE_ALLOWLIST`.
- **Never run `services/api-py` with its workers on against the live vaults, except the deployed instance.** Its listener refunds any `LOCKED` offramp missing from its own database, so a second backend cancels live payments. Use `WORKERS=0`, `--dry-run` or test keys.
- **Keep `PAYOUT_COMMAND` on the stub on the deployed backend.** The phone payer works independently, so a real provider there would pay the merchant twice.
- **Anything under `contracts/` changes only through Arc Studio** (session `scanpay-vault`, then `arc-studio pull`). `.arc-studio/pull-manifest.json` hashes those files as unedited Arc Studio output. The vaults aren't upgradeable, so a contract change means redeploying and updating `deployments/*.json`.
- **Arkiv entities are public.** UPI IDs, payee names, raw QR text, salts and UTRs stay off Arkiv. See `arkiv/schema.md`.

## Commands

Python projects use `uv` and Python 3.12. There's no CI and no configured Python linter, so run the checks below yourself.

| Project | Run | Test | One test |
|---|---|---|---|
| `services/api-py` | `uv run uvicorn app.main:app --port 8788` | `uv run pytest -m "not live"` | `uv run pytest tests/test_workers.py::test_failure_refunds_reason_3` |
| `apps/upi-payer` | `uv run python main.py run` | `uv run pytest` | `uv run pytest tests/test_agent.py::test_dry_run_uses_no_phone` |
| `agent-server` | `uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8790` | `uv run pytest` | `uv run pytest tests/test_agent.py::test_goal_to_paid_on_the_store` |
| `extension` | `npm run build`, then load `dist` unpacked in Chrome | `npx playwright test` | `npx playwright test -g "typing a prompt"` |
| `apps/web` | `npm run dev` | `npm test`, `npx playwright test` | `npx vitest run -t "splitSignature"`, `npx playwright test -g "dark mode"` |
| `apps/mock-store` (local only, not in git) | `python3 -m http.server 5180 --bind 127.0.0.1 --directory apps/mock-store` | Through `extension`, whose browser tests skip without it | |
| `contracts` | | `forge test` | `forge test --match-test test_Complete_OnlySettler` |

- **`services/api-py`:** `uv run pytest -m live` makes read-only calls to both testnets. `--dry-run` simulates transactions without broadcasting.
- **`apps/upi-payer`:** dry run unless `UPI_LITE_LIVE=1`. `main.py check` and `main.py rehearse --upi-id <id> --inr 1` can't tap Pay, so use them to calibrate a phone. `main.py check` needs its `.env`.
- **`agent-server`:** a run through the extension pays for real unless it's a rehearsal, and so does `app.demo_store`, which requires `--yes`. `uv run python -m tests.e2e_stack` starts the fake off-ramp the extension's tests use. Tests must never use a real `WalletAdmin`, because Circle allows only 5 agent wallets per chain.
- **`apps/web`:** `npm run build` also type-checks. Browser tests use installed Chrome at phone size. `e2e/live.spec.ts` sends real testnet transactions and runs only when its environment variables are set.
- **`contracts`:** run `git submodule update --init --recursive` first. `ARC_RPC_URL` and `WHITECHAIN_RPC_URL` enable the fork tests in `test/fork/`.

## How a payment flows

```
Wallet or agent --quote/offramp--> services/api-py --paymaster tx--> ScanPayVault (locks tokens)
                                        | listener matches the lock to its private quote
                                        | stub "pays", then the settler completes after 15 s
apps/upi-payer  --polls /events, reads the payee--> pays through Google Pay on an ADB phone
```

1. `POST /quote` validates the QR, signs `hashIntent` as quote signer, and stores `qrText` and `salt` privately. On-chain, only `qrCommitment = keccak256(abi.encode(salt, qrText))` appears.
2. The user signs the intent and a permit, and `POST /offramp` relays them, so the user pays no gas.
3. The listener matches `OfframpLocked` against the stored quote, and refunds on a mismatch or a bad QR.
4. Separately, the payer sees the event, derives the `viewToken`, reads the payee and on-chain amount, applies its local limits, and pays.
5. `LOCKED -> COMPLETED`, then after the 24-hour dispute window `RELEASED`. Disputes go `DISPUTED -> REVERSED | RELEASED`, and an unresolved dispute refunds the user after 7 days.

**`COMPLETED` doesn't prove the merchant was paid.** The backend isn't told the phone's result, so the two can disagree. The web app therefore says "Payment processed" and offers a dispute. Don't treat `COMPLETED` as paid in new code. Reporting results back is a known, parked gap.

## Things that must stay in step

There's no shared package between these projects, so a change on one side needs a matching change on the other:

- **`viewToken`:** `derive_view_token` in `services/api-py/app/quote_service.py` and `apps/upi-payer/agent.py` must match. Both test files hold the same test vector, and `VIEW_TOKEN_SECRET` must be the same on the server and the payer.
- **API paths on the shared hostname:** a new top-level route goes in `apps/web/deploy/Caddyfile` and `API_PATHS` in `apps/web/vite.config.ts`. The web app can't use those paths for its own pages.
- **UPI QR parsing:** `services/api-py/app/qr.py` has the final say, and `apps/web/src/lib/upi.ts` and `agent-server/app/payment/qr.py` mirror it.

## Design decisions that aren't obvious

- **Circle wallet:** it's a smart account, so signatures verify through ERC-1271 and not `ecrecover`. The backend checks permits with `ecrecover` only, and a valid permit would reset the allowance to one payment. So the wallet pays from a capped `approve` and sends a deliberately invalid permit, which the vault's `try/catch` ignores. Evidence is in `docs/verify-log.md`.
- **Quotes last 90 seconds.** Request them last, after every other check has passed.
- **`apps/upi-payer` return values:** `failed` must mean money can't have moved. Anything unclear after the Pay tap is `unknown`. It only taps Pay after reading the right amount, payee and "UPI Lite" on screen, and only starts from Google Pay's home screen.
- **`agent-server` is a plain state machine** (`app/machine.py`). Deciders only choose page actions. Code decides when the checkout is reached, by finding one UPI QR and one total on an allowed site, and `verify.py` checks the quote. A model can't cause a payment.
- **Agents last, and models can't change them.** They're kept in `agent-server/data/agents.sqlite`, and only the panel's requests write to them. Models never write memory, because a hostile page could then plant a lasting instruction. A run gets only code-written facts about past runs.
- **Each agent has its own Circle wallet, and its budget is that wallet's allowance to the vault.** `WalletAdmin` and `WalletManager` change budgets, so `machine.py` and `payment/` must never import them. A run can only sign.
- **Order-placing clicks** go ahead on their own only when code can see UPI selected and the total within the limit. They could otherwise spend through a saved card or cash on delivery, outside every limit here.
- **Clicks are real input through `chrome.debugger`,** because sites such as Swiggy ignore scripted clicks. It's attached only to the run's tab, after the site check, and only during a run.
- **`openai_base` can point at any service with OpenAI's API.** Agents' instructions, profiles and page text go there on every step, so choose it with that in mind.
- **A profile stays out of events, logs and webhooks.** `AgentRun.redact` hides values a model repeats, and `callbacks.py` never receives a profile.
- **The agent reports the payer's result, not `COMPLETED`.** `app/payment/results.py` reads the payer's ledger, read-only.
- **The agent server checks `Origin`.** Only the extension may use it from a browser, or any website could drive a server that can spend money.
- **Web wallets:** plain EIP-1193 and EIP-6963, with no web3 library, because the backend builds every typed-data payload. An installed PWA has no injected wallet, so phones open the app in MetaMask's browser.

## Deployment

One Azure host serves the web app and the API through Caddy. It runs copies of the code, not a git checkout, so `main` and the server can differ.

- **Backend:** `/opt/rupeerail`, under the `rupeerail` systemd service, with its database in `/var/lib/rupeerail`.
- **Secrets:** `/etc/rupeerail/`.
- **Web app:** `/var/www/rupeerail`. Steps are in `apps/web/README.md`.
- **`apps/upi-payer`:** runs on the machine with the phone attached, and needs Google Pay open and the phone unlocked. Tap coordinates are for a 1080x2408 screen.

## More detail

| File | Covers |
|---|---|
| `README-agents.md` | The long-lived agents plan |
| `docs/backend-notes.md`, `docs/verify-log.md` | Backend findings and tested answers for the plan |
| `arkiv/schema.md` | Arkiv payout board design |
| `README-backend.md` | API, environment variables, backend internals. It cites handoff files that aren't in the repo |
| `apps/web/README.md`, `apps/upi-payer/README.md` | Those apps' setup |
| `contracts/README.md` | Contract tests and deployment |
