# Arc Studio provenance: how to check the contract came from Arc Studio

`ScanPayVault` was written, audited, tested and deployed (on Arc) by **Arc Studio**. Every file under `contracts/` (except the `lib/` submodules) was produced by Arc Studio and pulled into this repo unedited. Claude Code built the tooling around it (smoke test, deployment records, docs) and deployed the Whitechain copy.

## 1. The Arc Studio project
- Project: https://studio.arc.io/app/27d96855-8adb-4904-ac5a-ff15aab2f8cc. The chat history, the code and the Contracts panel are all there.
- CLI session: `scanpay-vault` (`arc-studio` v1.2.2).
- Turn 1 wrote `ScanPayVault`, its mocks, a 172-test suite and the deploy scripts with the max preset (two audit agents plus Slither).
- Turn 2 deployed V2 on Arc testnet and verified it.

## 2. The files are unedited Arc Studio output
`.arc-studio/pull-manifest.json` is written by `arc-studio pull`. It records the SHA-256 of each file exactly as it came out of the Arc Studio sandbox. To re-check:

```bash
python3 -c 'import json,hashlib; m=json.load(open(".arc-studio/pull-manifest.json"))["entries"]; \
print([p for p,e in m.items() if hashlib.sha256(open(p,"rb").read()).hexdigest()!=e["hash"]] or "all files match Arc Studio")'
```

## 3. V2 on Arc was deployed by Arc Studio

| | |
|---|---|
| Vault | [`0x1203a10706290d5a3c62bd99d10186d909e89e39`](https://explorer.testnet.arc.io/address/0x1203a10706290d5a3c62bd99d10186d909e89e39), source verified |
| Deploy tx | [`0x17ae5edd…a26f`](https://explorer.testnet.arc.io/tx/0x17ae5edd3925d55ee1661aca01e6ef7d7399361a1143c9173ff772c5388ba26f) |
| Sent by | Arc Studio's platform account `0xB029E8EAee8F51c8B6feB933832F8052a6c9Dc4b`, through the ERC-4337 EntryPoint `0x5FF1…2789` (`handleOps`) |
| Owner / settler | `0xEe5658e3479BB162B9a0D45F03e3ac0066A00BC1`, the project wallet. The platform account has **no** rights |

**Bytecode check:** the deploy call contains this repo's compiled `ScanPayVault` creation code, all 13,528 bytes of executable code, followed by the constructor arguments `(Arc USDC, owner, settler)`. Only the trailing compiler-metadata hash differs, and that changes with build paths. Reproduce with `forge inspect ScanPayVault bytecode` against `cast tx <deployTx> input`.

## 4. Behaviour proven onchain
`scripts/smoke-test.sh <arc|whitechain> <vault>` sends real testnet transactions through the full lifecycle. The results are 34/34 checks on each chain, logged with explorer links in `deployments/*.smoke.log`. Every event (`OfframpLocked`, `OfframpCompleted`, `OfframpReversed`, `OfframpRefunded`, `PoolFunded`, `OnrampPaid`, `PaymentEmitted`, `PoolWithdrawn`) can be seen in the vault's **Logs** tab on the explorer.

## 5. Who did what

| Part | Built by |
|---|---|
| `contracts/src`, `contracts/test`, `contracts/script`, `foundry.toml` | Arc Studio |
| V2 deploy + verification (Arc) | Arc Studio |
| `contracts/lib` (OpenZeppelin v5.1.0, forge-std v1.16.2, pinned to Arc Studio's versions) | `forge install` |
| V1 deploy (Whitechain), `scripts/`, `deployments/`, docs | Claude Code |
