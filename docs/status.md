# Project status

Last updated 2026-11-06. Update this when something ships or a decision changes.

## Live

| Part | State |
|---|---|
| Contracts | Deployed on Arc testnet and Whitechain Sepolia |
| Backend | Deployed, serving quotes and settlement |
| Web app | Deployed and used in live demos |
| Phone payer | Runs on one laptop and phone, in live mode for demos |

## In progress: Arkiv and the agentic checkout

Plan: `README-agents.md`. We're targeting Arkiv's Build on Arkiv (from scratch) prize, with an agent payout board. Submission time is still to be confirmed on the ETHGlobal dashboard.

| Milestone | State |
|---|---|
| 0: checks | Circle and the off-ramp done. Jev needs a key. Swiggy deferred |
| 1: payment path | Done, except for the callback and the Arkiv receipt. A real ₹1 went from `agent-server` through the Circle wallet and the phone |
| 2: extension | Not started |
| 3: agent loop | Not started, needs Jev and OpenAI keys |
| 4 and 5: stitch and harden | Not started |
| Arkiv writes and reads | Schema drafted, needs an access key and test GLM |

## Decisions

- Arkiv holds a payout board: jobs, claims, receipts and payers. See `arkiv/schema.md`.
- The demo pays through a mock store first. Real Swiggy is a stretch goal, because the phone drops QR fields such as `tr`, and the backend refuses BharatQR.
- The Circle wallet pays from a capped allowance, with no permit. See `docs/verify-log.md`.
- Browser wallets only for now, with WalletConnect as a possible later addition.

## Parked

- **Reporting payout results to the backend.** Until then, `COMPLETED` can appear when nobody was paid.

## Needed from people

- [ ] Arkiv access key and test GLM.
- [ ] Jev and OpenAI API keys, in `agent-server/.env`.
- [ ] The backend's "section 5" rules, probably in `HANDOFF-payment-cycle-api-metamask.md`, which isn't in the repo.
- [ ] A public repo, required by the prize. Scan the history for secrets first.
- [ ] Consent from the owner of `apps/mock-store/upi.png` before publishing it, or a dedicated test QR instead. It's a personal UPI QR.
- [ ] New operator keys before any real funds. The current testnet seed and test wallet key have been exposed in a chat.
