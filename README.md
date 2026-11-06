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
