# Plan: Long-lived agents, each with its own Circle wallet

Make an agent that's good at one job, such as a Chef that knows your diet and address. Open the extension, choose it, and type a prompt. It browses in your own Chrome, orders, and pays the UPI QR through RupeeRail from its own smart wallet on Arc. It then reports whether the merchant was really paid.

**Pitch: "Autonomous, but bounded."** Each agent pays with no human step, but it can't spend beyond an on-chain budget of its own, and no model can move money.

**Status: agreed 2026-11-06.** Progress is in `docs/status.md`, and test results are in `docs/verify-log.md`.

**Prizes:**

- **Arc, Best Smart Contracts with Advanced Stablecoin Logic.** `ScanPayVault` is conditional escrow built in Arc Studio, and each agent is a smart account with its own on-chain spending cap.
- **Arkiv, Build on Arkiv (from scratch).** A public track record for each agent, and the payout board.

---

## 1. The experience

1. Open the side panel and see agent cards: avatar, name, what it does, budget left.
2. Choose **Chef**, and type "Something light for dinner".
3. The extension opens Chef's start page, or uses the current tab if it's already on one of Chef's sites.
4. The agent browses using its instructions and profile, and goes to checkout.
5. It selects UPI. Code reads the QR and total, Chef's wallet signs, the vault locks the funds and the phone pays.
6. The panel shows the payer's real result, and the run joins Chef's history.

An agent's avatar stays with it on the prompt screen, beside each live step, and in its history.

---

## 2. What an agent is

| Part | Where it lives | Written by |
|---|---|---|
| Name, avatar, instructions, allowed sites, start page, limit per order | `agent-server/data/agents.sqlite` | A person, in the panel |
| Profile: diet, address, preferences | The same file, mode 600 and git-ignored | A person, in the panel |
| Run history: prompt, site, amount, result, transaction | The same file | Code only |
| Wallet | Circle smart account on `ARC-TESTNET` | Created with the agent |
| Budget | USDC `approve` from the agent's wallet to the vault | A person, in the panel |

**Memory rule:** models never write to an agent's stored memory. Otherwise a hostile page could plant an instruction that lasts across every later run. Each run gets the profile and the last 5 real runs, written by code from the person's prompt, the site, the amount and the result.

**Avatars:** 12 SVG characters bundled in `extension/static/avatars/`, with 8 background colours. An agent stores only the two ids, and the server rejects anything else, so there are no uploads or remote images.

**One run at a time, across all agents,** because there's one phone and one driven tab. Schedules and background runs are out of scope.

---

## 3. Architecture

```
┌───────────────────────── Chrome (the user's own session) ─────────────────────────┐
│  Side panel: agents, prompt, live steps, history, kill switch                      │
│  Service worker: opens the start page, screenshots, enforces the agent's sites     │
│  Content script: reads the page in every frame, then clicks, types and scrolls     │
└───────────────▲──────────────────────────────────────────────┬─────────────────────┘
                │ observation                                  │ action
        ┌───────┴───────────── agent-server (localhost) ───────▼──────┐
        │  Store: agents and runs in SQLite                           │
        │  WalletManager and WalletAdmin: panel requests only         │
        │  State machine: observe → decide → act → ... → pay          │
        │  Decider: OpenAI, or rule-based for tests                   │
        │  verify.py: code-enforced checks, no model involved         │
        │  Signer: one Circle wallet per agent                        │
        │  Payout results: reads the payer's outcome                  │
        └──────┬───────────────────┬──────────────────────┬───────────┘
    quote, pay │       typed data  │       payer's result │
               ▼                   ▼                      ▼
     Off-ramp backend      Each agent's Circle wallet  apps/upi-payer ──adb──> Phone
               │                                          ▲
               ▼                                          │ polls /events
        ScanPayVault on Arc ──── OfframpLocked ───────────┘
```

### API

| Endpoint | Purpose |
|---|---|
| `GET /templates` | Templates, avatar and colour ids, and the server's limits |
| `GET, POST /agents` | List and create. A list leaves out instructions and profiles |
| `GET, PATCH, DELETE /agents/{id}` | Read, edit, archive |
| `POST /agents/{id}/budget` | Send a new `approve` |
| `POST /agents/{id}/fund` | Ask the faucet if the wallet is short, and retry setup |
| `GET /agents/{id}/runs` | History |
| `POST /agents/{id}/runs` | Start: `{prompt, max_inr?, rehearse?}` |
| `WS /runs/{id}`, `GET /runs/{id}`, `POST /runs/{id}/stop` | Live steps, state and the kill switch |

The server binds to `127.0.0.1` and rejects browser requests whose `Origin` isn't the extension. Otherwise any website could drive a server that can spend money.

---

## 4. Wallets

Each agent's spending is separately visible on-chain, and its budget is a cap the vault can't exceed whatever the model or server does.

**Circle's limits:** 5 agent wallets per chain, and `circle wallet create --testnet` also creates one on every other testnet. Archiving doesn't free a slot. So creation reuses an unassigned wallet first, and uses the agent's id as its idempotency key so a retry can't use a second slot.

**Setup, shown live on the agent's card:**

1. **Assign or create:** take an unassigned wallet, or create one.
2. **Fund:** ask the faucet if the balance is below the budget. If it refuses, the card shows the address so USDC can be sent to it.
3. **Set the budget:** `approve` the vault, then read the allowance back. It isn't called ready until the chain shows it.

Why the wallet pays from an allowance with a deliberately invalid permit is in `docs/verify-log.md`.

**Controls:** a new budget sends a new `approve`, capped by `AGENT_MAX_BUDGET_USDC`. Archiving sets the allowance to 0 first, and refuses to archive if that can't be confirmed. These actions live in `WalletAdmin`, which the state machine and deciders are never given. A run can only sign.

---

## 5. State machine

```
preflight → observe ─→ checkout detected by code ─→ pay → await payer's result → callback → done
               │
               ├→ stopped by the user ─→ halted
               ├→ step or time limit ──→ failed
               ├→ stuck or model down ─→ need_help (pause for a person)
               └→ decide → act → observe
```

- **Checkout:** code decides it has been reached, by finding one UPI QR and one clear total on an allowed site. A model saying `checkout_reached` isn't enough.
- **Order-placing clicks** such as "Place order" go ahead on their own only when UPI is visibly selected and the known total is within the limit. Otherwise they're blocked, and the model is told why. If UPI still can't be confirmed, a person is asked. Over the limit is refused, with no option to continue.
- **Adding items:** one item can only be added as many times as the prompt asks for, once by default. A real model added 27 of one dish before this guard, because each add changed the page and so hid the repetition. An add only counts once code sees a real sign of it, and the model is told when a click had no effect.
- **Clicks** are real input through `chrome.debugger`, because sites such as Swiggy ignore scripted clicks. It's attached only to the run's tab, after the site check, and only during a run, and Chrome shows its banner meanwhile.
- **Pauses:** login wall, CAPTCHA, OTP, or an ambiguous choice.
- **Model errors:** brief rate limits and outages wait and retry. A long limit, such as a daily one, pauses straight away and says when it resets.

### Rehearsal

A rehearsal browses normally, but can't place an order or request a quote. It stops at the first order-placing click or QR, and reports the site, the total, whether UPI was selected, whether a QR was readable, and any off-site hosts that may need adding to the agent's sites, such as a payment gateway. It doesn't need a wallet.

### Payer's result

On-chain `COMPLETED` isn't proof of payment, because the backend completes about 15 seconds after the lock whether or not the phone paid. So the agent reads the payer's own record:

| Payer's result | Panel says |
|---|---|
| `paid` | **Paid** |
| `failed` | **Not paid**, and that the funds can be disputed |
| `unknown` | **Check the phone** |
| `dry_run` | **Dry run**, no rupees moved |
| None after 2 minutes | **No result**, the payer may be offline |
| On-chain `REFUNDED` or `REVERSED` | **Not paid**, funds returned |

---

## 6. Safety

Enforced in code and never delegated to a model. `verify.py` runs after `/quote` and before signing:

1. The quote's payee matches the QR's `pa=`.
2. The quote's amount equals the page total, equals `am=` if present, and is within the run's limit.
3. The tab is on one of the agent's sites.
4. The quote and permit deadlines are still ahead, with a margin.
5. The signed payloads match `/config`: chain, vault, token, wallet and spender.
6. The rate is within 5% of market.
7. One payment per run.
8. The kill switch can stop anything before `/offramp`.

**Spending limits, in layers:**

| Layer | Limit |
|---|---|
| Agent's budget, on-chain | Its allowance. The vault can't pull more |
| Agent's limit per order | Set in the panel. A prompt can lower it, not raise it |
| Server ceiling | `AGENT_GLOBAL_MAX_INR`, default ₹1,000 |
| Backend | ₹1,000 per payment, ₹5,000 per wallet per day |
| Phone payer | Its own per-payment and daily caps across all agents, and the UPI Lite balance |

Circle's own spending policies are mainnet-only, so they aren't used.

**Against a malicious page:**

- Page text and candidates are marked to models as untrusted, and they're told never to follow instructions in them.
- Fixed rules come first in the system message, and an agent's own instructions can't change them.
- The agent has no "go to URL" action, and can only click elements in the current snapshot. The start page comes from the agent's stored settings.
- A hostile page on an allowed site can only influence clicks. It can't change limits, checks or anything stored.

**Privacy:**

- Instructions and profile go to OpenAI on every step, and the panel says so.
- They never go in events, logs, webhook payloads or Arkiv. Values from a profile that a model repeats in its reasoning show as `[from profile]`.
- The agent can type profile details into pages, but only on its allowed sites.

**Also:** a live payer pays any lock with real rupees, so automated tests use a fake off-ramp or an allowlisted payer.

---

## 7. Build order

0. **Baseline.** Done.
1. **Agents and runs.** Done.
2. **Per-agent wallets.** Done, including a real new wallet.
3. **Agent behaviour.** Done.
4. **Extension.** Done, and used from a person's own Chrome.
5. **Food test site.** Done: `apps/mock-store/foodie/`, kept locally.
6. **Real sites.** In progress. Amazon went from search to a real QR, a real lock and the phone, which stopped at a low UPI Lite balance. Swiggy reached its cart. See `docs/verify-log.md`.
7. **Arkiv, built.** See `arkiv/schema.md`. The original plan: A public `rupeerail.agent` card with name, avatar id, category, wallet and paid count, extended while the agent is active. Payout receipts linked to the agent's wallet. The panel shows each agent's track record, read through a server-side proxy. Personal data stays off Arkiv.
8. **Submission.** Architecture diagram, prize files, recorded run, secret scan, `upi.png` consent or replacement, AI-use statement, and confirming the Arc track.

---

## 8. Risks on real sites

None of these are verified yet, and any one could rule a site out:

| Risk | What happens | Fallback |
|---|---|---|
| Desktop checkout has no UPI QR, only "enter your UPI ID" or an app redirect. RupeeRail can only pay a QR or `upi://` link | The agent asks for help | Another site |
| The QR is BharatQR, which the backend refuses | Refused before any money moves | Another site, or add support to the backend |
| Google Pay's displayed merchant name fails the payer's screen check | The phone doesn't pay, and the funds need refunding or disputing | Rehearse a ₹1 payment to that merchant, if possible |
| A QR payment isn't matched to the order | The merchant is paid but the order may not confirm | Test once with a cheap order |
| The phone is locked, or the payer is offline | **Not paid** or **No result**, and test USDC stays locked until disputed | Unlock the phone before any live run |

**If no real food site works,** the demo runs on `apps/mock-store/foodie/` with a real ₹1 payment, and Amazon remains the real-site attempt.

---

## 9. Prize requirements

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
