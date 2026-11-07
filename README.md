# RupeeRail

Scan any UPI QR, pay in stablecoins, with zero gas for the user. A Solidity escrow vault holds the funds until the merchant has been paid in INR.

**Built in Arc Studio:** https://studio.arc.io/app/27d96855-8adb-4904-ac5a-ff15aab2f8cc

## Deployed contracts

| Chain | ScanPayVault | Token |
|---|---|---|
| Arc testnet (5042002) | [`0x878734374e6131fceeeb83fa8605ab49f4d21836`](https://explorer.testnet.arc.io/address/0x878734374e6131fceeeb83fa8605ab49f4d21836) | USDC |
| Whitechain Sepolia (1874) | [`0x326fa638bc40ebb605bedb86b990e8d3da4765c1`](https://explorer.testnet.whitechain.io/address/0x326fa638bc40ebb605bedb86b990e8d3da4765c1) | USDT |

## Smart contract (Solidity)

[`contracts/src/ScanPayVault.sol`](contracts/src/ScanPayVault.sol) · Solidity 0.8.28 · Foundry · OpenZeppelin v5.1.0

| Function | Who | What it does |
|---|---|---|
| `offramp(intent, userSig, quoteSig, permit)` | anyone (paymaster) | Locks the user's stablecoin. The user and the quote signer both sign the same EIP-712 intent; a permit means no approve tx |
| `completeOfframp(id, payoutRef)` | settler | After the INR payout: escrow → held funds, which payouts can't touch |
| `refundOfframp` / `reclaimOfframp` | settler / anyone after 24 h | Return a locked payment to the user |
| `disputeOfframp` / `disputeOfframpWithSig` | user / anyone with the user's signature | Freeze a completed payment within 24 h. The signed version is gasless |
| `resolveDispute` / `claimDisputeTimeout` | arbiter / anyone after 7 days | Settle a dispute. The timeout always refunds the user |
| `releaseOfframp` | anyone after the window | Held funds → pool |
| `onramp` · `fundPool` · `withdrawPool` · `sweepSurplus` | settler · anyone · owner · owner | Pool payouts and treasury (never escrow or held funds) |

- **Privacy:** no UPI data onchain, only `qrCommitment = keccak256(abi.encode(salt, qrText))`.
- **Roles:** owner (Ownable2Step), settler, quote signer, arbiter, guardian (pause-only).

```
offramp ─► LOCKED ─complete─► COMPLETED ─(24 h)─ release ─► RELEASED (pool)
             │                    ├─ reverse ─► REVERSED (user repaid)
             └─ refund/reclaim ─► REFUNDED     └─ dispute ─► DISPUTED ─ resolve / 7-day timeout ─► REVERSED | RELEASED
```

## Verified
- **Tests:** 374 Foundry tests (unit, fuzz, invariant) plus 22 fork tests against the real Arc USDC and Whitechain USDT.
- **Live end to end:** every function was exercised live on both vaults, including gasless lock and dispute, refund, reverse, both dispute outcomes, pool, sweep, guardian pause, and access-control reverts.

## Arkiv: public agent track records

Agents that opt in publish a public record to Arkiv on Rabdi (chain 7794095): a card, a read-only receipt for each payment, and a marker while they're working. [The Agents page](https://rupee-rail.indiasouthcentral.cloudapp.azure.com/agents) reads these from Arkiv, needs no wallet, and updates live.

**Writer wallet:** [`0xa0B88fd138CC23394091F46A64AAc4e38eA4Dd8c`](https://rabdi.explorer.arkiv.network/address/0xa0B88fd138CC23394091F46A64AAc4e38eA4Dd8c), which creates every entity.

**One query you can run**, with a free [access key](https://hub.arkiv.network/access-keys) and Node 22.10 or later. It needs no RupeeRail server:

```bash
cd services/arkiv-board && npm ci
ARKIV_ACCESS_KEY=<your key> npm run verify -- 0x179dfbC07B23541849a2Cb6D38d991D9Ec7Cda88
```

That runs this query, among others:

```ts
select().from(Receipt).createdBy("0xa0B88fd138CC23394091F46A64AAc4e38eA4Dd8c")
  .where(eq("wallet", addr("0x179dfbC07B23541849a2Cb6D38d991D9Ec7Cda88"))).orderBy(desc("finishedAt"))
```

Details: [schema](arkiv/schema.md), [why Arkiv](arkiv/why-arkiv.md), [how we use it](arkiv/how-you-use-arkiv.md), [reach](arkiv/reach.md), [friction](arkiv/friction.md).

## How AI was used

This project was built with Claude Code, which wrote much of the code under the team's direction, and the team reviewed and tested the results. At run time, agents use a vision model through an OpenAI-compatible API to choose page actions. The model can't cause a payment: plain code decides when a checkout is reached and checks every quote.

