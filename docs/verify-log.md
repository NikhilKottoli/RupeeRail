# Verify log

Answers to the plan's VERIFY list (`README-agents.md`, section 11). Started 2026-11-06.

| # | Question | Status | Answer |
|---|---|---|---|
| 1 | Does Swiggy web checkout expose a UPI QR? | **Deferred** | The demo uses a mock store first. A stretch goal. |
| 2 | Circle `--chain` for Arc testnet? | **Yes, tested** | `ARC-TESTNET` works for typed-data signing. |
| 2 | Are Circle signatures ECDSA-verifiable? | **No, tested** | It's a smart account. Signatures verify through ERC-1271 only. See below. |
| 2b | Does the backend support Arc? | **Yes, tested** | Live payments reached `COMPLETED`: [lock](https://explorer.testnet.arc.io/tx/0x39ce2c0deddafc11408cfcb41e427a0606ee5998c139d0ebc11ad893372b3060), [browser-signed lock](https://explorer.testnet.arc.io/tx/0xd8f08ae50152e8676f35ad6704a01a7816015ddf13b3c459a6070bebd0fd5258). |
| 3 | Jev endpoint, limits, confidence? | **Docs only** | See below. Latency needs a key. |
| 4 | Backend "section 5" rules? | **Open** | The document isn't in this repo. See `backend-notes.md`. |
| 5 | Quote and permit expiry? | **Yes, from code** | Both are 90 seconds. |
| 6 | Does `.click()` work on Swiggy? | **Deferred** | Only needed for the Swiggy stretch goal. |
| 7 | Can a Circle spending policy be set from the CLI? | **Docs: not on testnet** | `circle wallet limit` is "Mainnet only". Limits must be enforced in `verify.py`. |

## Milestone 0

- **0a, Swiggy:** deferred, by decision.
- **0b, Circle signing:** done with CLI 1.2.3. See below.
- **0c, Jev:** read the docs, but no call made because there's no key yet.
- **0d, off-ramp on Arc:** done through the Circle wallet, with the payer temporarily allowlisted. See below.

## Circle wallet

Agent wallet on Arc testnet: `0x179dfbc07b23541849a2cb6d38d991d9ec7cda88`.

**Setup.** It's a smart account and can't sign until deployed. The CLI returns "This wallet isn't deployed on-chain yet" before that. It was funded with 20 test USDC from `circle wallet fund`, then deployed by a [0.01 USDC self-transfer](https://explorer.testnet.arc.io/tx/0xb907e4cbdcb6ded401298e3f5f7f1de84127e079de77ad585351d40eef823194).

**Results, using harmless sample payloads:**

| Check | Result |
|---|---|
| Signature length | 65 bytes |
| `ecrecover` | Returns a different address each time, not the wallet |
| `isValidSignature` on the wallet | Valid (`0x1626ba7e`) for both the permit and the intent |
| `USDC.permit` simulated with `eth_call` on Arc | Accepted. A tampered signature was rejected, so the check is meaningful |

**What that means for the off-ramp, from reading the code:**

- **Intent:** should work. `verify_user_signature` in the backend and `SignatureChecker` in the vault both support ERC-1271.
- **Permit:** the backend checks it with `ecrecover` only, so it would return `BAD_PERMIT_SIG` unless the vault already has enough allowance.
- **A valid permit also breaks the next payment:** the vault would set the allowance to that one payment's amount, and pulling it leaves zero.

**Decision.** No backend change. The wallet pre-approves the vault up to a cap, and the agent sends a deliberately invalid permit, which the backend accepts because the allowance covers it and the vault ignores through its `try/catch`. The allowance then acts as an on-chain spending cap. To top it up or revoke it, call `approve` again.

**Tested end to end on 2026-11-06.** The agent read `upi.png` from a screenshot of the mock store, took the ₹1 total from the page, and paid from the Circle wallet with the invalid permit: [lock](https://explorer.testnet.arc.io/tx/0x5686e0c2edc313b89e9fac8caac7055e913c5ad7a9343ee2091b7b5363b9433f). The allowance fell by the token amount, so the permit was ignored as intended, and the phone paid ₹1 to the QR's payee.

An earlier attempt, offramp `0x4cb63e99…`, reached `COMPLETED` on-chain, but the phone didn't pay because the notification panel was covering Google Pay. That was fixed in `go_home`. It's also a real example of the parked gap: the chain said `COMPLETED` while the merchant got nothing.

## Jev

From docs.typesafe.ai:

- `POST https://api.typesafe.ai/v1/systemone` with `Authorization: Bearer <key>`.
- Body: `{model, state, questions}`. A `choice` question takes `criteria` with 1 to 255 options.
- A choice answer returns `choice`, `confidence` from 0 to 1, and `probabilities`.
- `GET /v1/models` lists available models. `jev-latest` is the documented model.
- Images need the `TypeSafe-Danger-Zone-Images: true` header and a vision-capable model. The plan's DOM-first approach still fits.

## Arkiv

- Rabdi testnet RPC answers: chain ID `0x76edaf` (7794095), 2-second blocks.
- `@arkiv-network/sdk` needs Node 22.10 or later. Node 22.20.0 is installed here, but the default is 20.19.4.
- The Python SDK on PyPI is `arkiv-sdk` 1.0.0b3.dev0, and its repo was last pushed in May 2026. The current docs list only the TypeScript SDK, so TypeScript is the supported path for writes.
