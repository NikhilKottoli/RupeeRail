# Project status

Last updated 2026-11-06. Update this when something ships or a decision changes.

## Live

| Part | State |
|---|---|
| Contracts | Deployed on Arc testnet and Whitechain Sepolia |
| Backend | Deployed, serving quotes and settlement |
| Web app | Deployed and used in live demos |
| Phone payer | Runs on one laptop and phone, in live mode for demos |

## In progress: long-lived agents

Plan: `README-agents.md`. Prize targets: Arc (Advanced Stablecoin Logic) and Arkiv (Build on Arkiv, from scratch).

| Step | State |
|---|---|
| 1: agents and runs | Done |
| 2: per-agent wallets | Done. Chef reuses the first wallet, and a second real wallet was created, funded, approved and checked on Arc |
| 3: agent behaviour | Done. Real models chose a vegetarian or a chicken dish from the profile alone, against a fake off-ramp |
| 4: extension | Done, and used from a person's own Chrome |
| 5: food test site | Done. The mock stores are kept locally, not in git |
| 6: real sites | In progress. Amazon reached a real QR, a real lock and the phone. Swiggy reached its cart. Zomato not tried |
| 7: Arkiv | First entity written, then parked until step 6 works |
| 8: submission | Not started |

**Live payments through the agent server:** no rupee payment has succeeded through an agent's wallet yet. A real Chef run stopped at a locked phone, and a real ₹256 Amazon run stopped when the phone's UPI Lite balance was too low. In both, the panel correctly said Not paid while the chain said `COMPLETED`, and those test USDC can be disputed. The earlier ₹1 through the first wallet and the phone was before the extension existed.

## Decisions

- Agents are long-lived: a name, avatar, instructions and profile, with a run history and their own Circle wallet.
- Models never write an agent's stored memory.
- The agent is fully autonomous under its limits, with a kill switch until submission.
- Success comes from the payer's result, not on-chain `COMPLETED`.
- The agent loop is a plain state machine in Python, using OpenAI through a `Decider` interface.
- The demo site is chosen by rehearsing on real sites, with `apps/mock-store/foodie/` as the fallback. The phone receives the merchant's raw QR, but the backend still refuses BharatQR.
- Order-placing clicks go ahead on their own only when UPI is visibly selected and the total is within the limit.
- Each Circle wallet pays from a capped allowance, with no permit. See `docs/verify-log.md`.
- Arkiv holds a payout board: jobs, claims, receipts and payers. See `arkiv/schema.md`.
- Browser wallets only for now, with WalletConnect as a possible later addition.

## Parked

- **Reporting payout results to the backend.** Until then, `COMPLETED` can appear when nobody was paid. The agent avoids this by reading the payer's result directly, but the backend and PWA don't yet.
- **The rest of the Arkiv board**, until the agent works.

## Needed from people

- [ ] Enough UPI Lite balance on the phone before any real order. The agent can't see that balance before it locks funds.
- [ ] Your own details in Chef's profile. It currently holds a test diet.
- [ ] A new model API key after the event. The current one has been exposed in a chat.
- [ ] The backend's "section 5" rules, probably in `HANDOFF-payment-cycle-api-metamask.md`, which isn't in the repo.
- [ ] A public repo, required by both prizes. Scan the history for secrets first.
- [ ] Removing `apps/mock-store/upi.png` from this branch's history before going public, or merging with a squash. It's a personal UPI QR, in earlier commits on the branch.
- [ ] New operator keys before any real funds. The current testnet seed and test wallet key have been exposed in a chat.
