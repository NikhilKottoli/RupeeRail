# Plan: Agentic checkout demo ("tell it what to buy, it buys it and pays from a wallet")

Hackathon demo, Circle is a sponsor. Live demo on real Swiggy, with a Playwright script as fallback.

---

## 0. Instructions for the coding agent (read first)

1. **Explore the existing off-ramp backend before writing anything.**
   - Find its repo or API docs and list every endpoint (method, path, request/response schema, auth, error codes).
   - Known endpoints (from the owner, verify against code):
     - `POST {BASE}/quote` `{chain, user, inr, qrText}` returns `{id, intent, quoteSig, payee, display, signing{permit,intent}}`
     - `POST {BASE}/offramp` `{chain, intent, userSig, permit{deadline,v,r,s}}` returns `{id, txHash, status:"SUBMITTED"}`
     - `GET {BASE}/offramp/{id}` poll until `COMPLETED` then `RELEASED`, or `REFUNDED` / `REVERSED`
   - **Chain change:** the backend was first set up on Whitechain testnet. The target chain is now **Arc testnet**. Check whether the `chain` field accepts Arc, which chain ID and token/permit contract it uses, and whether anything is hardcoded to Whitechain. Record this in `docs/backend-notes.md`. If the backend needs changes to support Arc, list them for the owner instead of editing existing endpoints.
   - The backend docs mention a "section 5" that says how to verify a quote locally (payee, amount, domains). Find it and implement exactly what it says in `verify.py`.
   - Note the quote expiry and permit `deadline` (owner thinks 5 to 10 minutes).
   - Write findings to `docs/backend-notes.md`.
2. **Do not change existing endpoints.** Add new endpoints or modules only where this plan says so. If the existing backend is Python and has a natural place for the agent server, say so in `docs/backend-notes.md` and put the agent server there. Otherwise create a new Python service.
3. Anything marked **VERIFY** is an assumption. Test it early (see section 10, Milestone 0) and record the result in `docs/verify-log.md`.
4. Keep everything behind small interfaces (`Decider`, `Signer`, `Executor`) so that fallbacks can be swapped in without rewrites.
5. Prefer boring and working over clever. This is a demo.

---

## 1. Goal and demo story

User types one goal in the extension, for example: _"Order a paneer butter masala under ₹300 on Swiggy."_

1. The agent wallet is **pre-funded** (Circle Agent Wallet on Arc testnet). There is no funding step in the demo.
2. Agent drives the user's own logged-in browser tab: search, pick item, add to cart, proceed to payment.
3. At the payment step the agent gets a UPI QR, decodes it, and asks the off-ramp backend for a quote.
4. Code (not the LLM) verifies the quote, the wallet signs two EIP-712 messages, the backend pays the UPI payee.
5. Agent polls until the payment is `COMPLETED` / `RELEASED`, then fires a callback.

Pitch line: **"Autonomous, but bounded."** The wallet has spending limits and a merchant allowlist; the LLM can never move money on its own.

---

## 2. Decisions already made

| Topic         | Decision                                                                                    |
| ------------- | ------------------------------------------------------------------------------------------- |
| Client        | Chrome extension (Manifest V3, TypeScript), runs in the user's own logged-in browser        |
| Backend       | Python (FastAPI) agent server, plus the already-built off-ramp backend                      |
| Orchestration | LangGraph (a state machine, not a linear chain)                                             |
| Site          | Real Swiggy, user already logged in with an address saved                                   |
| User input    | Specific text goal                                                                          |
| Models        | Jev (TypeSafe AI) first, OpenAI as fallback and for free-text                               |
| Payment       | Via the existing off-ramp endpoints; wallet signs; backend status is the source of truth    |
| Chain         | **Arc testnet** (Circle-supported). Replaces Whitechain.                                    |
| Funding       | Agent wallet is pre-funded before the demo. No funding or onramp flow to build.             |
| Out of scope  | Refund/reversal handling, login/OTP automation, multi-site generality beyond a mocked claim |

---

## 3. Architecture

```
┌────────────────────────── Chrome (user's own session) ───────────────────────────┐
│  Swiggy tab                                                                       │
│   ▲  content script: DOM snapshot → candidate actions; executes click/type/scroll │
│   │                                                                               │
│  Service worker: screenshot (captureVisibleTab), relay, tab control               │
│  Side panel: goal input, live step log, wallet balance, status, kill switch       │
└───────────────▲───────────────────────────────────────────────┬──────────────────┘
                │ observation (candidates + screenshot)         │ action
                │                                               ▼
        ┌───────┴────────────────── Agent server (Python, FastAPI + LangGraph) ──────┐
        │  graph: observe → decide → act → progress-check → … → checkout → pay      │
        │  Decider: Jev (choice over candidates)  |  OpenAI (vision / free text)     │
        │  QR decode (screenshot → upi:// string)                                    │
        │  verify.py  (code-enforced guardrails, no LLM)                             │
        │  Signer: Circle CLI subprocess  |  local-key fallback                      │
        │  status poller + callback dispatcher                                       │
        └───────────────┬──────────────────────────────────────┬────────────────────┘
                        │ quote / offramp / poll               │ sign typed-data
                        ▼                                      ▼
              Existing off-ramp backend                 Circle Agent Wallet (CLI)
              ({BASE}, Arc testnet)
```

Why this split: the extension is the "hands" (sees and touches the page), the agent server is the "brain" (decides, holds secrets, talks to model APIs and the wallet), and the wallet/off-ramp is a separate security boundary. API keys and signing never live in the extension.

---

## 4. Components

### 4.1 Chrome extension (TypeScript, MV3)

**Content script**

- Build a **candidate action list** from the DOM: visible, interactive elements (links, buttons, inputs, elements with click handlers or ARIA roles).
- Assign each a stable per-snapshot id (e.g. `data-agent-id="a17"`).
- Each candidate: `{id, role, text, ariaLabel, placeholder, href?, bbox, inViewport}`.
- Cap the list (about 40 to 60 items, nearest the viewport first) to keep decisions fast.
- Also extract short page text for context (headings, price lines, cart total if visible).
- Execute actions: `click(id)`, `type(id, text)`, `scroll(dir)`, `key(name)`, `wait(ms)`.
  - For `type`, use the native value setter and dispatch `input` and `change` events so React-style apps notice.
  - If plain `.click()` is ignored by the site, fall back to dispatching real mouse events. **VERIFY** on Swiggy. As a last resort use `chrome.debugger` (trusted events, but it shows a yellow banner).

**Service worker**

- `chrome.tabs.captureVisibleTab` for screenshots (JPEG, downscaled).
- Maintains a WebSocket to the agent server. Relays observations up and actions down.
- Enforces: only operate on allowlisted origins (see section 7).

**Side panel UI**

- Goal input, start/stop (kill switch always visible), step-by-step log with the last screenshot, wallet address and balance, payment status.

### 4.2 Agent server (Python, FastAPI + LangGraph)

New endpoints on the agent server (the off-ramp backend is untouched):

- `POST /sessions` start a session `{goal, constraints{max_inr, allowed_domains}}` returns `{session_id}`
- `WS /sessions/{id}` bidirectional channel: observations in, actions out, events out
- `POST /sessions/{id}/stop` kill switch
- `GET /sessions/{id}` current state and history
- `POST /webhooks/test` demo receiver for the final callback (optional)
- `GET /wallet` address and balance (via the signer)

### 4.3 Decider (model layer)

Interface:

```python
class Decider(Protocol):
    def choose_action(self, state_text: str, candidates: list[Candidate],
                      screenshot: bytes | None) -> Action: ...
    def write_text(self, instruction: str, context: str) -> str: ...
```

**Primary: Jev (TypeSafe AI).** From public docs and community pages (**VERIFY against docs.typesafe.ai/api**):

- Jev is a "System One" model that returns **typed decisions** (a choice, a score, a yes/no probability) rather than free text. It is called via its own evaluate endpoint, with a `state` string and `questions` containing `choice` options with per-option criteria. It is fast and cheap.
- Natural fit: put the page summary and goal in `state`, and make each candidate action a choice option. Jev returns which one to take.
- A community project ("Jev Ultrafast") reportedly does exactly this for browser control, calling a text model only when text must be typed.
- Vision (image input, bounding boxes) was reported as an experimental opt-in in Jev 1.14, so treat Jev as **DOM-first** and don't depend on its vision for the demo.

**Fallback and free-text: OpenAI vision model.**

- Used when (a) Jev's confidence is low, (b) no candidate fits, (c) the page is a canvas or image-heavy, or (d) text must be written (e.g. a search query).
- Sends screenshot plus candidate list, returns the same `Action` schema.

Routing rule: try Jev. If confidence is below a threshold or the same state repeats twice, escalate to OpenAI with the screenshot. Log which model decided each step (nice for the demo).

`Action` schema:

```json
{
  "type": "click|type|scroll|key|wait|done|need_help|checkout_reached",
  "target": "a17",
  "text": "paneer butter masala",
  "reason": "short string"
}
```

### 4.4 Payment module

Triggered when the graph reaches `checkout_reached` and a QR is visible.

1. **Get `qrText`.**
   - First check the DOM for a `upi://` link or string (cheap, exact).
   - Else decode the QR from the latest screenshot (OpenCV `QRCodeDetector` or `pyzbar`). Retry with a fresh screenshot a few times; QR may load late.
2. **Get the amount to pay** from the cart/checkout total on the page (via the decider) and from the UPI string's `am=` parameter. They must agree.
3. `POST {BASE}/quote` with `{chain, user, inr, qrText}`. Request this only when the QR is on screen and everything else is ready, to avoid expiry.
4. **Verify** (section 7). Abort on any failure.
5. **Sign** `signing.permit` and `signing.intent` (EIP-712) via the Signer. Split the permit signature into `v, r, s`.
6. `POST {BASE}/offramp` with `{chain, intent, userSig, permit{deadline,v,r,s}}`.
7. Poll `GET {BASE}/offramp/{id}` every 2 to 3 s with a timeout (shorter than the quote expiry). `COMPLETED` then `RELEASED` means success. `REFUNDED` / `REVERSED` means fail, show it, and stop (no retry in the demo).
8. Fire the callback (4.6).

### 4.5 Wallet signer

Interface:

```python
class Signer(Protocol):
    def address(self) -> str: ...
    def sign_typed_data(self, typed_data: dict) -> str: ...  # returns 0x signature
```

**Primary: Circle Agent Wallet via Circle CLI**, called as a subprocess from the agent server (the CLI is Node, so it can't run inside the extension):

```bash
npm install -g @circle-fin/cli
circle wallet sign typed-data '<json>' --address 0xWallet --chain <CHAIN>
```

Circle's docs say typed-data signing is supported on EVM chains, and Circle lists Arc testnet as a supported chain. **VERIFY (test first):**

- Find the exact `--chain` identifier the CLI uses for Arc testnet (check the CLI help and Circle docs; don't guess it).
- The backend needs a standard ECDSA `v, r, s` permit. Confirm the agent wallet's signature over the permit typed data (with Arc's chain ID in the domain) verifies via `ecrecover`. A smart-contract-account wallet might not.
- Confirm the wallet's address is the same `user` address the backend expects, and that the permit's token (USDC on Arc testnet) supports the permit the backend builds.

**Fallback:** `LocalKeySigner` using `eth_account` with a testnet-only key from env. Same interface. If the Circle path works, use it as the headline; if not, say so honestly and keep the Circle story to the spending policy and funding.

**Spending policy:** if the Circle agent wallet supports caps and allowlists, set a small per-transaction and daily cap. Regardless, `verify.py` enforces its own caps in code.

### 4.6 Callback

On `COMPLETED` / `RELEASED`:

- Emit an event to the side panel (toast plus status).
- POST to a configurable webhook:

```json
{
  "session_id": "...",
  "status": "PAID",
  "offramp_id": "...",
  "txHash": "...",
  "amount_inr": 312,
  "payee": "...",
  "goal": "...",
  "finished_at": "ISO-8601"
}
```

Optional nicety: also screenshot the Swiggy confirmation page as extra proof, but don't depend on it.

---

## 5. Agent loop (LangGraph state machine)

State: `goal, constraints, step_count, history, last_observation, last_action, repeat_count, phase, qr_text, quote, payment_status`.

Nodes:

```
observe → decide → act → progress_check ─┐
   ▲                                      │
   └──────────────────────────────────────┘   (loop)

progress_check routes to:
  stuck        → escalate model (Jev → OpenAI), then → need_help
  checkout     → extract_qr → quote → verify → sign → submit → poll → callback → done
  step limit   → fail
  user stop    → halt
```

Rules:

- **Preflight at session start:** check the pre-funded wallet's balance (via `GET /wallet`). Refuse to start if it is below the session's `max_inr` equivalent (plus any fees).
- Hard step limit (e.g. 40) and a time limit.
- **Repeat detection:** if the same page state and action repeat twice, escalate; if still stuck, stop and report.
- Pause and ask the user (side panel) for: login wall, CAPTCHA, OTP, ambiguous item, price over cap.
- Phases: `search → select_item → cart → checkout → payment`. Include the phase in `state` text sent to the decider so choices stay focused.
- Stop browsing when the payment page with the UPI option is reached; hand off to the payment module.

---

## 6. Message contracts (extension ↔ agent server)

Observation (up):

```json
{ "type": "observation", "url": "...", "title": "...", "page_text": "...",
  "candidates": [ { "id": "a17", "role": "button", "text": "Add", "bbox": [x,y,w,h], "inViewport": true } ],
  "screenshot_b64": "..." }
```

Action (down): the `Action` schema in 4.3. Result (up): `{ "type": "action_result", "ok": true, "error": null }`.

Send the screenshot only when needed (Jev needs none; OpenAI fallback and QR decoding do).

---

## 7. Safety checks (code-enforced, never delegated to the LLM)

`verify.py` runs after `/quote` and before signing. All must pass:

1. Whatever "section 5" of the backend docs requires (payee, amount, domains). Implement exactly.
2. `payee` in the quote matches the `pa=` (VPA) in the decoded UPI string.
3. `inr` in the quote equals the `am=` in the UPI string, equals the page's cart total, and is at most `max_inr` for the session and the global cap.
4. The active tab's origin is in `allowed_domains` (e.g. `swiggy.com`).
5. Quote and permit deadline are still in the future with a safety margin.
6. The quote has not already been submitted (idempotency, one payment per session).
7. A kill switch can abort at any point before `/offramp`.

Other rules: signing and keys only on the agent server; never put keys or model API keys in the extension; log every decision, but not secrets.

---

## 8. Feature list

**Must (demo core)**

- Extension with side panel, goal input, live log, kill switch
- DOM candidate extraction plus click/type/scroll execution on Swiggy
- LangGraph loop with Jev decisions and OpenAI fallback
- QR decode, quote, verify, sign, submit, poll
- Wallet balance display (wallet is pre-funded, no funding flow)
- Success callback (webhook plus UI)

**Should (makes it convincing)**

- Spending cap and merchant allowlist (in code and in the wallet policy if supported)
- Per-step model attribution in the log (which model decided what)
- Ask-the-user pause on ambiguity or errors
- Playwright fallback script that replays a known-good path

**Could (only if time remains)**

- Confirmation-page screenshot as proof
- A second site via a mock store, to back the "any site" pitch
- Order history in the side panel

**Won't (this version)**

- Funding or onramp flow (wallet is pre-funded)
- Login/OTP automation, CAPTCHA solving
- Refund/reversal handling
- Multi-user accounts, production-grade key custody

---

## 9. Risks and mitigations

| Risk                                                                                                                              | Mitigation                                                                                                                                 |
| --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **Swiggy checkout may not show a UPI QR** (could be UPI ID entry, app redirect, or QR only on some views)                         | Milestone 0: do the checkout manually on desktop and record what appears. If no QR, change the plan or the demo site before building more. |
| Circle wallet signature isn't accepted by the backend (smart account, wrong domain/chain ID), or the backend isn't set up for Arc | Test first (0b, 0d); fall back to `LocalKeySigner` behind the same interface; ask the backend owner to support Arc                         |
| Pre-funded wallet runs out of balance or fees mid-demo                                                                            | Preflight balance check at session start; refuse to start below the session cap                                                            |
| Swiggy UI changes or blocks automation                                                                                            | Playwright fallback; keep the demo path rehearsed; pre-set address and cart basics                                                         |
| Agent loop is slow                                                                                                                | Jev for most steps (fast); cap candidates; screenshots only when needed                                                                    |
| Quote or permit expires                                                                                                           | Request the quote last; poll with a timeout; fail clearly                                                                                  |
| Bad decision moves money                                                                                                          | All payment checks in code; one payment per session; low caps; testnet                                                                     |
| Jev's vision is experimental                                                                                                      | Use Jev on DOM candidates only; OpenAI for vision                                                                                          |
| Live-demo failure                                                                                                                 | Pre-recorded run and the Playwright fallback ready                                                                                         |

---

## 10. Build order

**Milestone 0: spikes (do these first, about 1 to 2 hours total)**

- [ ] **0a.** Manually go through Swiggy checkout on desktop. Record payment options and whether a UPI QR or `upi://` appears. Save screenshots in `docs/`.
- [ ] **0b.** Run `circle wallet sign typed-data` on Arc testnet with a dummy permit payload (Arc chain ID in the domain). Find the right `--chain` value, and check `ecrecover` validity.
- [ ] **0c.** Make one call to Jev's evaluate endpoint (choice question over 5 sample candidates). Record latency and format.
- [ ] **0d.** Walk the existing off-ramp endpoints end to end on **Arc testnet** with a hardcoded QR string. Confirm the backend accepts Arc and you can reach `COMPLETED`. Use a local-key signer here if the Circle signer isn't ready yet.

**Milestone 1: payment path without the browser.** Agent server takes a `qrText` and goes quote → verify → sign → submit → poll → callback. Test with a QR image file.

**Milestone 2: extension skeleton.** Side panel, service worker, content script; take a screenshot, produce a candidate list, execute a click from the server.

**Milestone 3: agent loop.** LangGraph graph with Jev and OpenAI decisions; reach the Swiggy cart from a goal.

**Milestone 4: stitch.** Checkout to QR to payment to callback, live on Swiggy.

**Milestone 5: hardening and demo.** Caps and allowlist, ask-the-user pauses, repeat detection, Playwright fallback, recorded backup run, demo script.

---

## 11. VERIFY list (record answers in `docs/verify-log.md`)

1. Does Swiggy web checkout expose a UPI QR or `upi://` string? (0a)
2. What `--chain` value does the Circle CLI use for Arc testnet, and are the agent wallet's signatures ECDSA-verifiable by the backend? (0b)
   2b. Does the off-ramp backend support Arc testnet (chain ID, token/permit contract), or is it hardcoded to Whitechain? (0d)
3. Jev evaluate endpoint format, latency, limits on number of choices, and confidence output (0c)
4. Exact verification rules in the backend's "section 5" (0d)
5. Quote and permit expiry windows
6. Whether plain `.click()` works on Swiggy or real mouse events are needed
7. Whether a Circle spending policy can be applied to the wallet from the CLI

---

## 12. Suggested repo layout

```
/extension        # TypeScript MV3: content script, service worker, side panel
/agent-server     # Python: FastAPI, LangGraph graph, deciders, payment, signer, verify
  app/graph.py
  app/deciders/{jev.py,openai_vision.py,router.py}
  app/payment/{qr.py,offramp_client.py,verify.py,poller.py}
  app/signers/{circle_cli.py,local_key.py}
  app/callbacks.py
/fallback         # Playwright script for the known-good Swiggy path
/docs             # backend-notes.md, verify-log.md, demo-script.md, screenshots
plan.md
```
