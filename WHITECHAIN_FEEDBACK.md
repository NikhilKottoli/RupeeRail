# Whitechain feedback

## What worked

- Whitechain behaved like plain EVM. The same Foundry-compiled `ScanPayVault` bytecode used on Arc deployed unchanged with viem, and Foundry fork tests ran against the real test USDT.
- The test USDT supports EIP-2612 permits, which is what makes RupeeRail's gasless flow possible: the payer signs an intent and a permit, and the RupeeRail paymaster submits the transaction and pays the WBT.
- Gas costs were low and predictable, so the paymaster can cover them from a small spread.
- Contract verification on the Whitechain explorer worked, and the public RPC handled the backend's event polling (`eth_getLogs` in block ranges) without problems.

## Where the team got stuck

- The USDT's EIP-712 permit domain (name "Tether USD", version "1") had to be read from the token contract, because the docs do not list it. A wrong domain fails silently as an invalid signature.
- No embedded wallet or smart-account provider that RupeeRail already used (Circle wallets, Circle Swap Kit) supports Whitechain Sepolia. So the AI-agent wallets and the in-app swap run on Arc only, and Whitechain payers use a browser wallet.

## What the docs were missing

- A token page for the test USDT listing its decimals, permit domain, and whether it supports EIP-3009 `transferWithAuthorization`.
- A list of wallet, smart-account and paymaster providers that support chain ID 1874.
- Guidance on funding a relayer with WBT for a gasless flow: faucet limits, and recommended gas settings for relayed transactions.
