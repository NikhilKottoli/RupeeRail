# ScanPayVault

Single-token ERC-20 escrow and INR payout pool, one vault per chain; no native-currency payments. Self-contained Foundry project: solc 0.8.28, Paris EVM, vendored OpenZeppelin v5.1.0 and forge-std.

## Layout and checks

`src/ScanPayVault.sol` is the vault; `script/Deploy.s.sol` logs deployed configuration, including `disputeTimeout`; `lib/` holds dependencies. `test/` and `test-integration/` are maintained separately. From `contracts/`, source/script-only build: `forge build --skip 'test/**'`. Existing tests need updates for the new API before running the full suite.

## Roles and trust

| Role | Authority |
| --- | --- |
| Owner | Two-step ownership; cannot renounce. Sets roles/config, pauses/unpauses, withdraws free pool funds, sweeps surplus. |
| Settler | Completes within the permitted interval, refunds locked escrow, reverses completed funds during the window, pays unique onramp orders from the free pool. |
| Quote signer | ECDSA-signs the same intent digest as the user, authenticating id, user, amount, INR amount, rate, commitment and expiry. |
| Arbiter | Resolves disputed held funds strictly before the snapshotted deadline: original-user refund or pool release. |
| Guardian | Optional; can only pause, idempotently. Zero disables it. |
| User / anyone | User signs locks and opens disputes. Anyone can relay locks, fund the pool, reclaim expired locks, release eligible completed funds, or call `claimDisputeTimeout` to refund the original user. |

The chain cannot prove INR payment; the settler reports it and the arbiter adjudicates. The owner can replace roles immediately: use a multisig separated from the settler. Token blocklists may prevent a user's refund until unblocked; other users are unaffected.

## Lifecycle and accounting

- User and quote signer sign the same EIP-712 `hashIntent`, domain `ScanPayVault` / `1`; ERC-1271 user wallets work. Quotes require nonzero `inrPaise`, `rateE6` and `qrCommitment`. Use random ids, short expiries and `qrCommitment = keccak256(abi.encode(salt, qrText))` with an unpredictable secret salt; neither salt nor raw payee text goes onchain. Failed permits require sufficient allowance.
- Lock snapshots completion/reclaim deadlines and the dispute window. Completion requires `completableAt <= now < reclaimableAt` and a unique nonzero payout reference. Anyone may reclaim still-locked escrow at/after `reclaimableAt`, only to its original user.
- Completion moves escrow to held funds and fixes `releasableAt = completedAt + locked dispute window`. Before release opens, the settler may reverse or the user may dispute; afterwards anyone may release an undisputed completion to the pool.
- Dispute snapshots `resolveDeadline = now + disputeTimeout`, overwriting `Deadlines.releasableAt` without adding storage fields. For a `DISPUTED` offramp, `deadlines(id).releasableAt` returns that resolve deadline. Later timeout changes do not affect it. Before the deadline the arbiter may refund (`REVERSED`) or release to the pool (`RELEASED`). At/after it the arbiter cannot resolve; anyone may call `claimDisputeTimeout(id)`, setting `REVERSED` and returning held funds **only to the original user**, never the caller. Disputes block ordinary release/reverse/refund/reclaim.
- `balance >= escrowedTotal + heldTotal + poolBalance`. Onramps and owner withdrawals use only free pool funds; held funds never go directly to the settler. Surplus sweep sends only unaccounted balance; zero surplus is a silent no-op. Direct transfers are surplus, not funding. Deposits must be exact; fee-on-transfer/rebasing tokens are unsupported.
- Only `offramp` and `onramp` stop while paused. Settlement, recovery (including permissionless `claimDisputeTimeout`), arbitration and treasury operations remain available. Only the owner unpauses.

## Defaults and bounds

Defaults: max offramp 100 tokens; onramp cap 1000 tokens per fixed 24-hour window (not rolling/UTC); completion delay 15 s; reclaim delay 24 h; dispute window 24 h; **disputeTimeout 7 days**. Limit changes do not reset consumed cap. New-lock bounds: completion <= 1 day, reclaim 1 hour–30 days, dispute window 1 hour–30 days, with `completeDelay + 1 hour <= reclaimDelay`. The dispute window is not signed but has a 1-hour floor. Owner-only `setDisputeTimeout` accepts **1–30 days**, affecting only newly opened disputes.

## Deployment configuration

Required nonzero addresses: `TOKEN`, `OWNER`, `SETTLER`, `QUOTE_SIGNER`, `ARBITER`. Optional `GUARDIAN` defaults to disabled; `MAX_OFFRAMP_AMOUNT` and `DAILY_EMIT_CAP` default to constructor limits when zero/unset. Optional settings apply only when the broadcaster is `OWNER`; otherwise the script logs owner-action warnings. Execution of the deploy script is a separate, explicitly authorized operation; no deployment is performed by editing these files.
