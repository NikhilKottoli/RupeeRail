# Arkiv friction report

Everything here happened while building RupeeRail's track records.

**Versions:** `@arkiv-network/sdk` 1.0.1, viem 2.57.16, Node 22.20.0, Rabdi (chain 7794095), Arkiv MCP and plugin 0.5.4, November 2026.

## Issues

### 1. The TypeScript-only SDK forced a second runtime

- **Surface:** SDK, docs.
- **Expected:** to write from our agent server, which is Python.
- **Actual:** PyPI's `arkiv-sdk` is `1.0.0b3.dev0`, its repo was last pushed in May 2026, and the current docs list only TypeScript. We run a separate Node service beside the agent server, plus a Node install on a server that had none.
- **Reproduce:** `pip index versions arkiv-sdk --pre`, then compare with the SDK list in the docs.
- **What would fix it:** a maintained Python SDK, or a statement that the Python one is unsupported, with a worked JSON-RPC example for writing an entity.

### 2. The SDK needs a newer Node than the current default on many machines

- **Surface:** SDK install.
- **Expected:** to run under the machine's default Node, 20.19.4.
- **Actual:** the SDK needs Node 22.10 or later. Each command here has to select Node 22.
- **Reproduce:** `node -v` showing 20.x, then install and run the SDK's example.
- **What would fix it:** a clear engine error at startup, and the requirement near the top of the quickstart.

### 3. There's no public way to estimate a create or batch

- **Surface:** SDK, write-safety guidance.
- **Expected:** a method such as `estimateBatch(batch)`, because the guidance requires an unsigned estimate before every write.
- **Actual:** we run the real write against a wallet client with a custom transport, capture `eth_estimateGas`, and block signing. That's `estimateWrite` in `services/arkiv-board/src/arkiv.ts`, and about 30 lines for something every careful writer needs.
- **Reproduce:** look for a create or batch estimator in the 1.0.1 declarations.
- **What would fix it:** a public estimator that returns gas without any signer.

### 4. The recommended gas floor is far above what writes use

- **Surface:** write-safety guidance, Rabdi.
- **Expected:** a floor near real use.
- **Actual:** we followed the floor of 1,000,000 gas plus 400,000 per extra operation. Measured use was much lower:

| Write | Operations | Gas cap sent | Gas used | Transaction |
|---|---|---|---|---|
| Create a card | 1 | 1,000,000 | 170,252 | [`0x2a16bd…`](https://rabdi.explorer.arkiv.network/tx/0x2a16bdaeb248cd3e75db78bca4e2e2a8d896ef34349c9c2bbdaa8b762c1d4544) |
| Create the `live` marker | 1 | 1,000,000 | 109,014 | [`0xc6c9f3…`](https://rabdi.explorer.arkiv.network/tx/0xc6c9f38399365898cbe7936e0ffa92aaf2974fe840d1ef4c4c7bfcbb1495e2df) |
| Delete the `live` marker | 1 | 1,000,000 | 91,335 | [`0x7da618…`](https://rabdi.explorer.arkiv.network/tx/0x7da6180bd1d6983177f4451f7bb058f5135e7952f88cc99a2c2566e7e388d4bb) |
| Receipt | 1 | 1,000,000 | 211,193 | [`0xc93fcd…`](https://rabdi.explorer.arkiv.network/tx/0xc93fcdf63bbea01b0f061c62f1ecc4b2c8505d96ab025dc3a74c43a8bea24e60) |
| Receipt and lifetime extension | 2 | 1,400,000 | 230,464 | [`0xfa2265…`](https://rabdi.explorer.arkiv.network/tx/0xfa2265589c8a194f57ec4fce1157918a5565dfa5ff114fdca59b51b50c9734ed) |

  We weren't harmed, because unused gas isn't charged and test GLM is cheap, but a balance check against the cap can refuse a write that would have been affordable.
- **What would fix it:** publishing why the floor exists, and when it can be lowered.

### 5. The older event's rules are easy to find and look current

- **Surface:** docs, event material.
- **Expected:** one rulebook for ETHGlobal Mumbai.
- **Actual:** `Arkiv-Network/global-tour-stop-pre-devcon` describes a contest that closed on 18 October, on Tiramisu with SDK 0.8. It says there's no server-side `orderBy` and a limit of 200 per page, while SDK 1.0.1 has `orderBy` and 100 per page. We nearly planned around the old details.
- **What would fix it:** a note at the top of that repo saying it's closed, and linking to the current network and SDK.

## Tested paths that worked

| Surface | What we did | Expected | Happened |
|---|---|---|---|
| `executeBatch` | A create, a delete and an extension in one batch | All land together | They did, and the card's expiry moved |
| `watchEntityEvents` over a WebSocket | A write, then watched our feed | A push without polling | `EntityCreated` and `EntityDeleted` arrived. Events carry the owner, so we ignore other projects' traffic without any lookup |
| Compound query | `wallet`, `outcome` and a `u64` range together, ordered by `finishedAt` | Only matching rows | Correct, with both receipts unfiltered and the one paid receipt filtered |
| `createdBy` | Every read | Only the publisher's entities | Correct. `count()` works with and without it |
| Faucet and access key | Funded a wallet and made a key at the Hub | Enough to write | About 0.001 GLM of the 2 GLM was used for the first eight writes |
