# Verify log

Answers to the agent plan's open questions. Started 2026-11-06.

| # | Question | Status | Answer |
|---|---|---|---|
| 1 | Does Swiggy web checkout expose a UPI QR? | **Deferred** | The demo uses a mock store first. A stretch goal. |
| 2 | Circle `--chain` for Arc testnet? | **Yes, tested** | `ARC-TESTNET` works for typed-data signing. |
| 2 | Are Circle signatures ECDSA-verifiable? | **No, tested** | It's a smart account. Signatures verify through ERC-1271 only. See below. |
| 2b | Does the backend support Arc? | **Yes, tested** | Live payments reached `COMPLETED`: [lock](https://explorer.testnet.arc.io/tx/0x39ce2c0deddafc11408cfcb41e427a0606ee5998c139d0ebc11ad893372b3060), [browser-signed lock](https://explorer.testnet.arc.io/tx/0xd8f08ae50152e8676f35ad6704a01a7816015ddf13b3c459a6070bebd0fd5258). |
| 4 | Backend "section 5" rules? | **Open** | The document isn't in this repo. |
| 5 | Quote and permit expiry? | **Yes, from code** | Both are 90 seconds. |
| 6 | Does `.click()` work on Swiggy? | **Deferred** | Only needed for the Swiggy stretch goal. |
| 7 | Can a Circle spending policy be set from the CLI? | **Docs: not on testnet** | `circle wallet limit` is "Mainnet only". Limits must be enforced in `verify.py`. |

## Milestone 0

- **0a, Swiggy:** deferred, by decision.
- **0b, Circle signing:** done with CLI 1.2.3. See below.
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

## Agent wallets

Tested on 2026-11-07 through the running agent server, with real Circle and the live backend.

| Check | Result |
|---|---|
| Reusing an unassigned wallet | Chef took `0x179d…da88` with no new wallet |
| `circle wallet create --testnet` | New wallet `0x7d6d…0c95` for Stationer. This leaves 3 of Circle's 5 on Arc testnet |
| `circle wallet fund` on a new wallet | 20 test USDC arrived within 10 seconds |
| `approve` from a new wallet | Worked first time and deployed the smart account, with 133 bytes of code on Arc afterwards. No separate self-transfer was needed, and the balance stayed at 20 USDC |
| Signatures from the new wallet | `isValidSignature` returned valid for a sample permit and intent |
| Allowance as a per-agent budget | A real Chef run took its allowance from 5 to 4.989864 USDC, and Stationer's stayed at 1 |

**Not yet shown:** a lock from the new wallet, or a rupee payment from either wallet through the agent server. The phone was locked during that Chef run (`isKeyguardShowing=true`), so the payer reported `couldn't get Google Pay to its home screen`. The panel said Not paid while the chain said `COMPLETED`, which is the known gap, handled as designed. That offramp, `0xc4ee6263…`, holds about 0.01 test USDC and can be disputed.

## OpenAI limits

Tested on 2026-11-06 with an OpenAI key. It allowed 50 requests per day for each model, read from `x-ratelimit-limit-requests` on `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5-mini`, `gpt-5-nano`, `gpt-4.1-mini`, `gpt-4o-mini` and `gpt-4o`. An order takes about 8 to 25 requests.

| Model | With the real extension on `foodie`, against a fake off-ramp |
|---|---|
| `gpt-4.1-mini` | Vegetarian profile got Paneer butter masala, chicken profile got Chicken biryani, each in about 25 seconds |
| `gpt-5.4-mini` | Completed `bazaar` and a vegetarian order, before candidates had surrounding text |
| `gpt-5-mini` | Added 27 of one dish before the add guard existed. It correctly refused an over-limit dish |

## Amazon

Tested logged out, in a fresh profile, against a fake off-ramp. The real model searched `amazon.in`, added a pen to the cart and proceeded to checkout, using the extension's scripted clicks. It then asked for help at the sign-in page. Whether a logged-in checkout offers a usable UPI QR hasn't been tested.

## Swiggy

First real run on 2026-11-07, from a person's own Chrome, logged in, with `gpt-5-nano`. The agent set the location, found a restaurant and opened its menu, then failed to add anything:

- **Identical buttons:** the menu's "ADD" buttons sat in cards with long descriptions, so they had no surrounding text and looked like one item. The add cap then blocked a second dish as "already in the cart", and the model wrongly decided the cart was over the limit. Fixed.
- **The click had no effect:** Bhel Puri's ADD button stayed unchanged. Clicking the innermost element didn't help on a second run, while a person's click on the same button worked, so Swiggy most likely ignores scripted clicks. Clicks now go through `chrome.debugger` as real input. `apps/mock-store/foodie/` ignores scripted clicks in the same way, and passes. This hasn't been re-run on Swiggy, which returns a bot check to a fresh headless browser, so it can only be tested from a person's Chrome.
- **A false "added":** clicking scrolls the page and reorders the candidates, which the first add check took as proof of an add. It now looks for real signs: the button turning into a quantity control, the cart changing, a new page or an options dialog.

**Rehearsal loop, 2026-11-07, from a person's Chrome, with `gpt-4o-mini`:** real clicks work on Instamart: the "+" opened a pack dialog, and ADD and Confirm changed the item total to ₹72 and ₹93. Dialogs now list only their own controls. The window needs to be in front: Chrome's capture hung when it wasn't, so runs now raise it once and fall back to the debugger's capture. The model then kept adding different snacks, until its 50 requests for the day ran out, so code now sends it to the cart after 3 items. No run has reached Swiggy's cart or payment page yet.

## NVIDIA models

Tested on 2026-11-07 through `https://integrate.api.nvidia.com/v1`. `nvidia/nemotron-3.5-super-vl-120b-a12b` takes screenshots and is the one in use. `nvidia/nemotron-3-ultra-550b-a55b` refuses images, so the agent falls back to text only. Both completed the chicken order on `foodie`, in 31 and 46 seconds. Grok through a third-party service also worked, but ignored the structured-output request, so the agent now asks for JSON in the prompt as well, and tolerates text around it.

## Amazon checkout

Rehearsed on 2026-11-07 in a person's logged-in Chrome, with `nvidia/nemotron-3.5-super-vl-120b-a12b`. The agent searched, opened a product, added it, proceeded to checkout, selected **"Scan and Pay with UPI"**, confirmed it, and stopped at "Pay with UPI" with an order total of ₹256. So Amazon's desktop checkout does offer a UPI QR option. Still unknown: whether the QR reads, the backend accepts it, Google Pay's payee name passes the payer's check, and Amazon matches the payment to the order. Those need a real run.

**First real run, the same day:** the agent placed the ₹256 order and read the QR, which pays `amazon@rapl`. The backend accepted it, and Stationer's wallet locked 2.6 USDC. The payer then refused before tapping Pay, with `UPI Lite not shown as the payment source`, because the phone's UPI Lite balance was too low. No rupees moved. Offramp `0xdbf48fb1…` can be disputed. So Amazon's QR isn't BharatQR. Still unknown: Google Pay's payee name check, and whether Amazon matches the payment to the order.

An invisible "Add to cart shift + alt + K" keyboard control is still offered to the model. It wasted one click, and the agent then found the real button on its own.

## Arkiv

- Rabdi testnet RPC answers: chain ID `0x76edaf` (7794095), 2-second blocks.
- `@arkiv-network/sdk` needs Node 22.10 or later. Node 22.20.0 is installed here, but the default is 20.19.4.
- The Python SDK on PyPI is `arkiv-sdk` 1.0.0b3.dev0, and its repo was last pushed in May 2026. The current docs list only the TypeScript SDK, so TypeScript is the supported path for writes.
