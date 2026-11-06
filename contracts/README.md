# ScanPayVault

Escrow and payout pool for scan-and-pay offramps (stablecoin to INR) and onramps. One vault per chain; the token is fixed at deploy. The vault only uses the ERC-20 interface (no native-currency payments), so it is safe on Arc where USDC is also the gas coin.

## Layout

- `src/ScanPayVault.sol`: the contract.
- `test/*.t.sol`: unit, permit, fuzz tests. `test/invariant/`: handler-based invariants. `test/mocks/`: test tokens (6/18 decimals, no-return, fee-on-transfer, blocklisting).
- `test/fork/`: fork tests against the real Whitechain USDT and Arc USDC.
- `script/Deploy.s.sol`: deploy script.
- `lib/`: vendored `forge-std` and OpenZeppelin v5.1.0 (Paris-compatible, no PUSH0/MCOPY).

If you copy the project without `lib/`, reinstall:

```
forge install foundry-rs/forge-std OpenZeppelin/openzeppelin-contracts@v5.1.0
```

(Add `--no-git` if the folder is not inside a git repository.)

## Build and test

```
cd contracts
forge build
forge test                 # fork tests are skipped unless their RPC env vars are set
forge test --gas-report
```

## Fork tests

Set the RPC URLs (fork tests skip cleanly when a variable is missing):

```
export WHITECHAIN_RPC_URL=<Whitechain Sepolia RPC, chain 1874>
export ARC_RPC_URL=<Arc testnet RPC, chain 5042002>
forge test --match-path 'test/fork/*' -vv
```

Optional `PRIVATE_KEY`: if set, that key is the user, owner and settler and must hold at least 0.1 of the real token on that chain (otherwise the test is skipped). Without it, a test key is funded with `deal`.

Note: the Arc fork test etches test-only emulators for Arc's native-coin precompiles, because a plain Foundry fork cannot run them. It checks the permit domain and the vault logic, not Arc's real blocklist or precompile gas.

## Deploy

```
export TOKEN=<ERC-20 address>
export OWNER=<owner address>        # use a multisig or a safe key
export SETTLER=<settler address>
# optional (base units of the token; 0 or unset keeps the contract default)
export MAX_OFFRAMP_AMOUNT=100000000
export DAILY_EMIT_CAP=1000000000

forge script script/Deploy.s.sol --rpc-url <RPC> --private-key $DEPLOYER_KEY --broadcast
```

The limits are applied only if the broadcasting key equals `OWNER`; otherwise the script logs a warning and the owner must call `setLimits`. Defaults: max offramp 100 tokens, daily cap 1000 tokens, complete delay 15 s, dispute window 24 h, reclaim delay 24 h.

## Environment variables

| Name | Used by | Purpose |
| --- | --- | --- |
| `TOKEN`, `OWNER`, `SETTLER` | Deploy script | Constructor arguments |
| `MAX_OFFRAMP_AMOUNT`, `DAILY_EMIT_CAP` | Deploy script (optional) | Override default limits |
| `WHITECHAIN_RPC_URL`, `ARC_RPC_URL` | Fork tests | RPC endpoints to fork |
| `PRIVATE_KEY` | Fork tests (optional) | Real-token signer; never printed |

## Behavior notes

- The daily cap shared by `onramp` and `emitPayment` is a fixed 24-hour window that starts at deploy and restarts with the first payout after it ends.
- `reclaimDelay` and `disputeWindow` are fixed per offramp when it is locked; later owner changes only affect new offramps. `completeDelay` is read live.
- Offramp ids are first-come and not tied to a user. Someone watching a pending relayed transaction can lock their own offramp under the same id, so the relayer should submit privately and refund any unexpected `OfframpLocked`.
- `emitPayment` has no id and pays twice if retried; use `onramp` for real orders.
