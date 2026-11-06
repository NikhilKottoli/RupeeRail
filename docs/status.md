# Project status

Last updated 2026-11-06. Update this when something ships or a decision changes.

## Live

| Part | State |
|---|---|
| Contracts | Deployed on Arc testnet and Whitechain Sepolia |
| Backend | Deployed, serving quotes and settlement |
| Web app | Deployed and used in live demos |
| Phone payer | Runs on one laptop and phone, in live mode for demos |

## In progress: the agentic checkout

Final plan: `README-agents.md`. Prize targets: Arc (Advanced Stablecoin Logic) and Arkiv (Build on Arkiv, from scratch).

| Milestone | State |
|---|---|
| 0: checks | Done. Swiggy deferred |
| 1: payment path | Done, with payer results and the callback. A real ₹1 went from `agent-server` through the Circle wallet and the phone, before the extension existed |
| 2: extension | Built. Tested in Chromium against a fake off-ramp |
| 3: agent loop | Built. OpenAI drove the real extension on the mock store, against a fake off-ramp |
| 4: stitch | Works against a fake off-ramp. The first live run is still to do |
| 5: Arkiv board | First entity written, then parked until the agent works |
| 6: hardening and submission | Not started |

## Decisions

- The agent is fully autonomous under its limits, with a kill switch until submission.
- Success comes from the payer's result, not on-chain `COMPLETED`.
- The agent loop is a plain state machine in Python, using OpenAI through a `Decider` interface.
- The demo pays through a mock store first. Real sites are a stretch goal. The phone now receives the merchant's raw QR, but the backend still refuses BharatQR.
- Order-placing clicks such as "Place order" pause for a person, because they could spend through a saved card or cash on delivery.
- The Circle wallet pays from a capped allowance, with no permit. See `docs/verify-log.md`.
- Arkiv holds a payout board: jobs, claims, receipts and payers. See `arkiv/schema.md`.
- Browser wallets only for now, with WalletConnect as a possible later addition.

## Parked

- **Reporting payout results to the backend.** Until then, `COMPLETED` can appear when nobody was paid. The agent avoids this by reading the payer's result directly, but the backend and PWA don't yet.
- **The rest of the Arkiv board**, until the agent works.

## Needed from people

- [ ] The backend's "section 5" rules, probably in `HANDOFF-payment-cycle-api-metamask.md`, which isn't in the repo.
- [ ] A public repo, required by both prizes. Scan the history for secrets first.
- [ ] Consent from the owner of `apps/mock-store/upi.png` before publishing it, or a dedicated test QR instead. It's a personal UPI QR.
- [ ] New operator keys before any real funds. The current testnet seed and test wallet key have been exposed in a chat.
