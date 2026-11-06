# Plan: Agentic checkout

Tell an agent what to buy. It shops in your own browser tab, reads the UPI QR at checkout, pays from its Circle wallet on Arc, and reports whether the merchant was really paid.

**Pitch: "Autonomous, but bounded."** The agent pays with no human step, but it can't spend beyond an on-chain allowance, and no model can move money.

**Status: final, agreed 2026-11-06.** Progress is tracked in `docs/status.md`. Test results are in `docs/verify-log.md`.

**Prizes:**

- **Arc, Best Smart Contracts with Advanced Stablecoin Logic.** `ScanPayVault` is conditional escrow built in Arc Studio, paid by an agent.
- **Arkiv, Build on Arkiv (from scratch).** The payout board.

---

## 1. Demo story

The user types: _"Order a cutting chai."_

1. The Circle wallet on Arc testnet is already funded and has approved the vault up to a cap.
2. The agent drives the user's own tab on the mock store: search, add to cart, proceed to payment.
3. At the payment page, it reads the QR from a screenshot and the total from the page.
4. Code verifies the quote, the wallet signs, and the backend locks the funds in escrow.
5. The phone payer pays the merchant from UPI Lite.
6. The agent reads the payer's result, then shows and sends the outcome.

The mock store is a normal checkout that knows nothing about the agent, and its static QR has no amount, as on many real sites. Real sites such as Amazon are a stretch goal (section 9).

---

## 2. Decisions

| Topic | Decision |
|---|---|
| Autonomy | **Fully autonomous** under the limits. No confirmation before paying. The kill switch works until submission |
| Client | Chrome extension (Manifest V3, TypeScript) in the user's logged-in browser |
| Agent server | Python and FastAPI, in `agent-server/`, on the same laptop as the extension |
| Orchestration | **A plain state machine** in Python |
| Model | **OpenAI**, with vision. Behind a `Decider` interface |
| Without a key | A rule-based decider, for tests and the mock store. It's labelled as not AI |
| Payment | The existing off-ramp endpoints, which aren't changed |
| Wallet | Circle agent wallet, on `ARC-TESTNET`, `0x179dfbc07b23541849a2cb6d38d991d9ec7cda88` |
| Chain | Arc testnet |
| Success | **The payer's result, not on-chain `COMPLETED`** |
| Demo site | Mock store first |
| Payee in tests | `apps/mock-store/upi.png` |
| Order-placing clicks | **Pause for a person.** "Place order", "Buy now" and similar could spend through a saved card or cash on delivery, outside every limit |
| Out of scope | Funding flows, login and OTP automation, CAPTCHA solving, multi-user accounts |

---

## 3. Architecture

```
┌───────────────────────── Chrome (the user's own session) ─────────────────────────┐
│  Store tab: content script reads the page and clicks, types and scrolls            │
│  Service worker: screenshots, relays messages, enforces allowed domains            │
│  Side panel: goal, live step log, wallet balance, status, kill switch              │
└───────────────▲──────────────────────────────────────────────┬─────────────────────┘
                │ observation                                  │ action
        ┌───────┴───────────── agent-server (localhost) ───────▼──────┐
        │  State machine: observe → decide → act → ... → pay          │
        │  Decider: OpenAI, or rule-based                             │
        │  verify.py: code-enforced checks, no model involved         │
        │  Signer: Circle CLI                                         │
        │  Payout results: reads the payer's outcome                  │
        └──────┬───────────────────┬──────────────────────┬───────────┘
    quote, pay │       typed data  │       payer's result │
               ▼                   ▼                      ▼
     Off-ramp backend      Circle agent wallet       apps/upi-payer ──adb──> Phone
               │                                          ▲
               ▼                                          │ polls /events
        ScanPayVault on Arc ──── OfframpLocked ───────────┘
```

The extension is the hands, the server is the brain, and the wallet and off-ramp are a separate security boundary. Keys and signing never live in the extension.

---

## 4. Components

### 4.1 Chrome extension (`extension/`)

**Content script**

- Lists up to 60 visible interactive elements, nearest the viewport first, each with a per-snapshot id: `{id, role, text, ariaLabel, placeholder, value, href, bbox, inViewport}`.
- Skips password fields, and sends short page text for context.
- Scrolls a likely QR image into view when the page mentions UPI, so the screenshot contains it.
- Actions: `click`, `type`, `scroll`, `key`, `wait`. Typing uses the native value setter and dispatches `input` and `change` events, and clicks dispatch real mouse events.

**Service worker**

- Takes screenshots with `chrome.tabs.captureVisibleTab`.
- Keeps a WebSocket open to the server, with a ping every 20 seconds so Chrome doesn't shut the worker down.
- Stays on the tab the session started in, and refuses to act on any origin outside `allowed_domains`.

**Side panel**

- Goal input, start and stop, and a kill switch that's always visible.
- A step log with the last screenshot, wallet address and balance, and payment status.
- It reports the payer's actual result (section 4.5).

### 4.2 Agent server (`agent-server/`)

| Endpoint | Purpose |
|---|---|
| `POST /sessions` | Start: `{goal, max_inr, allowed_domains?}` returns `{session_id, allowed_domains}` |
| `WS /sessions/{id}` | Observations in, actions and events out |
| `POST /sessions/{id}/stop` | Kill switch |
| `GET /sessions/{id}` | State and history |
| `GET /wallet` | Address, balance and remaining allowance |
| `POST /webhooks/test` | Demo receiver for the callback |

It binds to `127.0.0.1` and rejects browser requests whose `Origin` isn't the extension. Otherwise any website the user visits could connect to the WebSocket and drive a server that can spend money.

### 4.3 Decider

```python
class Decider(Protocol):
    def choose_action(self, state_text: str, candidates: list[Candidate], screenshot: bytes | None) -> Action: ...
    def write_text(self, instruction: str, context: str) -> str: ...
```

- **`OpenAIDecider`:** sends the state, the candidate list and a screenshot, and gets back an `Action` through a JSON schema. The model comes from `OPENAI_MODEL`.
- **`RuleDecider`:** matches the goal against labels such as "Add", "Cart" and "Proceed". It lets the whole loop run without a key, and the log says it isn't AI.

```json
{ "type": "click|type|scroll|key|wait|done|need_help|checkout_reached", "target": "a17", "text": "cutting chai", "reason": "short string" }
```

Each step logs which decider chose it.

### 4.4 Payment module (`agent-server/app/payment/`, already built)

1. Reads the QR from the page: an exact `upi://` link first, otherwise the screenshot.
2. Reads the total from the page. If the QR has `am=`, the two must agree.
3. Checks the origin and limits, then requests a quote. **Quotes last 90 seconds**, so this comes last.
4. Runs `verify.py`, and aborts on any failure.
5. Signs the intent through the Circle CLI.
6. Sends a deliberately invalid permit with `POST /offramp`, so the vault pays from the allowance. A valid permit would reset the allowance to a single payment. See `docs/verify-log.md`.
7. Polls `GET /offramp/{id}`.

### 4.5 Payer's result

On-chain `COMPLETED` isn't proof of payment. The backend completes about 15 seconds after the lock, whether or not the phone paid.

So the agent waits for the payer's own record of that offramp, behind an interface:

```python
class PayoutResults(Protocol):
    def result(self, offramp_id: str) -> Literal["paid", "failed", "unknown", "dry_run"] | None: ...
```

- **First implementation:** reads the payer's ledger, `apps/upi-payer/data/payer.sqlite`, read-only, since both run on the same laptop.
- **After Milestone 5:** reads the `rupeerail.receipt` entity from Arkiv, and the agent no longer needs to share a machine with the payer.

| Payer's result | Agent reports |
|---|---|
| `paid` | **PAID** |
| `failed` | **NOT PAID**, and that the funds can be disputed |
| `unknown` | **CHECK THE PHONE** |
| `dry_run` | **DRY RUN**, no rupees moved |
| None after 2 minutes | **NO RESULT**, the payer may be offline |
| On-chain `REFUNDED` or `REVERSED` | **NOT PAID**, funds returned |

### 4.6 Wallet signer (already built)

```python
class Signer(Protocol):
    uses_allowance: bool
    def address(self) -> str: ...
    def sign_typed_data(self, typed_data: dict) -> str: ...
```

- **`CircleCliSigner`:** the default. It's a smart account, so it verifies through ERC-1271 and pays from an allowance.
- **`LocalKeySigner`:** a testnet-only fallback, which signs permits.

### 4.7 Callback

A POST to a configurable webhook, and a toast in the side panel:

```json
{ "session_id": "...", "status": "PAID", "offramp_id": "...", "txHash": "...", "amount_inr": 1, "payee": "...", "goal": "...", "finished_at": "ISO-8601" }
```

### 4.8 Arkiv payout board (`services/arkiv-board/`)

Design in `arkiv/schema.md`. Jobs, claims, receipts and payer heartbeats, each owned by the wallet that acted and expiring when no longer useful. UPI details stay off Arkiv.

To qualify, users must see data read from Arkiv:

- The side panel and PWA show "Merchant paid" from a `paid` receipt from a known payer.
- Both show whether a payer is online, from its heartbeat entity.
- The PWA has a public board of open jobs and active payers.

Reads go through a server-side proxy, because access keys must not reach a browser.

---

## 5. State machine

State: `goal, constraints, step_count, history, last_observation, last_action, repeat_count, phase, payment`.

```
preflight → observe ─→ checkout detected by code ─→ pay → await payer's result → callback → done
               │
               ├→ stopped by the user ─→ halted
               ├→ step or time limit ──→ failed
               ├→ stuck ───────────────→ need_help (pause for the user)
               └→ decide → act → observe
```

- **Preflight:** refuses to start if the wallet's balance or allowance can't cover `max_inr`.
- **Limits:** 40 steps, plus a time limit.
- **Repeat detection:** the same page and action twice gets a screenshot sent to the model. If it's still stuck, the session pauses and asks the user.
- **Pauses:** login wall, CAPTCHA, OTP, or an ambiguous item. A price over the limit ends the session as refused.
- **Checkout:** code decides it has been reached, by finding one UPI QR and one clear total on an allowed origin. A model saying `checkout_reached` isn't enough.

---

## 6. Messages between the extension and server

Observation:

```json
{ "type": "observation", "url": "...", "title": "...", "page_text": "...",
  "candidates": [ { "id": "a17", "role": "button", "text": "Add", "bbox": [0, 0, 10, 10], "inViewport": true } ],
  "screenshot_b64": "..." }
```

Action: the schema in 4.3. Result: `{ "type": "action_result", "ok": true, "error": null }`.

---

## 7. Safety

Enforced in code and never delegated to a model. `verify.py` runs after `/quote` and before signing, and is already built and tested.

1. The quote's payee matches the QR's `pa=`.
2. The quote's amount equals the page total, equals `am=` if present, and is within `max_inr`.
3. The tab's origin is in `allowed_domains`.
4. The quote and permit deadlines are still ahead, with a margin.
5. The signed payloads match `/config`: chain, vault, token, wallet and spender.
6. The rate is within 5% of market.
7. One payment per session.
8. The kill switch can stop anything before `/offramp`.

**Spending limits, in layers:**

| Layer | Limit |
|---|---|
| Vault allowance, on-chain | 5 USDC today. The vault can't pull more |
| `max_inr`, per session | Set when the session starts |
| Global cap | `AGENT_GLOBAL_MAX_INR`, default ₹30 |
| Phone payer | Its own per-payment and daily caps, and the UPI Lite balance |

Circle's own spending policies are mainnet-only, so they aren't used.

**Against a malicious page:**

- Page text is marked to models as untrusted data, and they're told never to follow instructions in it.
- The agent has no "go to URL" action, and it can only click elements that exist in the current snapshot.
- A hostile page on an allowed origin can only influence clicks. It can't change the limits or the checks.
- Reaching a checkout on a non-allowed origin stops the session.

**Also:**

- A live payer pays any lock with real rupees, so automated tests use a fake off-ramp or an allowlisted payer.
- Decisions are logged, but secrets aren't.

---

## 8. Prize requirements

**Arc:**

- [x] Built in Arc Studio, with a shareable project URL in `README.md`
- [ ] Working frontend and backend
- [ ] Architecture diagram
- [ ] Demo video showing Arc Studio, with a presentation and documentation
- [ ] State the bounty being submitted for

**Arkiv:**

- [ ] Writes to Rabdi, and a feature users see reads from Arkiv
- [ ] README with the writer wallets and one runnable query
- [ ] `arkiv/schema.md`, `friction.md`, `reach.md`, `why-arkiv.md`, `how-you-use-arkiv.md`

**Both:**

- [ ] A public repo with an open-source license, after a secret scan
- [ ] State how AI was used
- [ ] Replace `apps/mock-store/upi.png` or get its owner's consent before going public

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| A real site may not offer a UPI QR on desktop | Check its checkout by hand once before relying on it |
| The backend rejects BharatQR, and a real site's QR may be one | The agent refuses it before paying. Check by hand first |
| Google Pay shows a different name from the QR's, so the screen check stops the payment | It fails safely. Rehearse with that merchant before a demo |
| UPI Lite's balance is low, and a real order costs more than it holds | Top up before any real order |
| The phone's screen state breaks the tap sequence | The payer returns to home first, and refuses to tap otherwise. Leave the phone alone during a demo |
| The payer is offline, so a lock completes with nobody paid | The agent reports NO RESULT. Payer heartbeats arrive with Milestone 5 |
| Quote expires | Request it last |
| Model is slow or makes bad choices | Cap the candidates, keep the rule-based decider as a demo fallback |
| A real site blocks automation or ignores synthetic clicks | It's a stretch goal |
| Live demo failure | A recorded backup run |

---

## 10. Build order

**Milestone 0: checks.** Circle signing and the off-ramp on Arc are done. Swiggy deferred.

**Milestone 1: payment path.** Built and tested with a real ₹1. Left to do: payer results and the callback.

**Milestone 2: extension skeleton.** Side panel, service worker and content script. It takes a screenshot, lists candidates, and runs a click sent by the server.

**Milestone 3: agent loop.** The state machine with the rule-based decider, working on the mock store. Then the OpenAI decider, once a key is available.

**Milestone 4: stitch.** Goal to payment to payer's result to callback, live on the mock store.

**Milestone 5: Arkiv board.** Publisher, payer claims and receipts, and heartbeats. Then the proxy and the screens that read from Arkiv. Switch `PayoutResults` to Arkiv.

**Milestone 6: hardening and submission.** Pauses and repeat detection, the architecture diagram, prize files, a recorded run, and the demo script.

**Stretch:** a real site such as Amazon, and an automatic dispute when the payer reports `failed`.

---

## 11. Open questions

Answers go in `docs/verify-log.md`.

1. Does a real site's web checkout, such as Amazon's, show a usable UPI QR? Only needed for the stretch goal.
2. What are the backend's "section 5" rules? The document isn't in this repo. `verify.py` uses the checks in section 7 until it arrives.
3. Which OpenAI model gives the best speed for this? Test once there's a key.
4. Does the extension's screenshot read the QR reliably at normal window sizes? Test in Milestone 2.

---

## 12. Repo layout

```
/extension              TypeScript MV3: content script, service worker, side panel
/agent-server           Python: FastAPI, state machine, deciders, payment, signers, results
/services/arkiv-board   TypeScript: Arkiv writes
/apps/mock-store        The demo checkout
/apps/upi-payer         Phone payer
/apps/web               PWA
/docs                   Status, verify log, backend notes
/arkiv                  Prize files
```
