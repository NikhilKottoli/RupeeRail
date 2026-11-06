#!/usr/bin/env bash
# Live smoke test for a deployed ScanPayVault (gate step 4 in CLAUDE.md).
# Usage: scripts/smoke-test.sh <whitechain|arc> <vaultAddress>
# Sends real testnet transactions from the SEED_PHRASE wallet (index 0), acting as user, owner and settler.
# The test is designed to be token-neutral: every token that leaves the wallet comes back, and only gas is spent.
# On Arc, gas is paid in the same USDC as the payments, so wallet checks add back the gas this script spent.
set -euo pipefail

CHAIN="${1:?chain: whitechain|arc}"; VAULT="${2:?vault address}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "$CHAIN" in
  whitechain) RPC=https://rpc.testnet.whitechain.io; EXPL=https://explorer.testnet.whitechain.io
              EXP_TOKEN=0xcedfB56113143d55b2358b8CFbB0dEB5Ad2f55a7; TVER=1; GAS_IN_TOKEN=0; GASFLAGS=() ;;
  arc)        RPC=https://rpc.testnet.arc.io;        EXPL=https://explorer.testnet.arc.io
              EXP_TOKEN=0x3600000000000000000000000000000000000000; TVER=2; GAS_IN_TOKEN=1; GASFLAGS=(--gas-price 25gwei) ;;
  *) echo "unknown chain $CHAIN"; exit 1 ;;
esac
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
OFFRAMP_SIG='offramp((bytes32,address,uint256,uint64,uint64,string,uint64),bytes,(uint256,uint8,bytes32,bytes32))'
OFFRAMPS_GETTER='offramps(bytes32)(address,uint96,uint64,uint64,uint64,uint8,bytes32)'

# Derive the key without sourcing .env (the phrase is unquoted) and never print it.
_M="$(sed -n 's/^SEED_PHRASE=//p' "$ROOT/.env" | tr -d '"'"'")"
PK="$(cast wallet private-key --mnemonic "$_M")"; unset _M
ME="$(cast wallet address --private-key "$PK")"

TOKEN="$(cast call "$VAULT" 'token()(address)' --rpc-url "$RPC")"
CHAIN_ID="$(cast chain-id --rpc-url "$RPC")"
TNAME="$(cast call "$TOKEN" 'name()(string)' --rpc-url "$RPC" | tr -d '"')"
VAULT_LC="$(tr 'A-F' 'a-f' <<<"$VAULT")"

AMT=100000          # 0.1 token (6 dp) per offramp, about ₹10
INR=1000            # ₹10.00 in paise
RATE=99338880       # ₹99.33888 per token, recorded only
QR_OK='upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point&cu=INR'
QR_BAD='hello-not-a-upi-qr'
mkdir -p "$ROOT/deployments"; LOG="$ROOT/deployments/${CHAIN}.smoke.log"; : > "$LOG"
PASS=0; FAIL=0; GAS_WEI=0; NTX=0

say()  { echo "$*" | tee -a "$LOG"; }
ok()   { PASS=$((PASS+1)); say "  ✅ $*"; }
bad()  { FAIL=$((FAIL+1)); say "  ❌ $*"; }
check(){ local label="$1"; shift; if "$@"; then ok "$label"; else bad "$label"; fi; }
num()  { awk '{print $1}'; }
bal()  { cast call "$TOKEN" 'balanceOf(address)(uint256)' "$1" --rpc-url "$RPC" | num; }
# Wallet balance with this script's gas added back (only matters on Arc, where gas is USDC).
wbal() { local b; b="$(bal "$ME")"; if [ "$GAS_IN_TOKEN" = 1 ]; then echo $(( b + GAS_WEI / 1000000000000 )); else echo "$b"; fi; }
TOL=$(( GAS_IN_TOKEN ? 2 : 0 ))   # ±2 base units of rounding on Arc (18 → 6 dp truncation)
near() { local d=$(( $1 - $2 )); [ "${d#-}" -le "$TOL" ]; }
vget() { cast call "$VAULT" "$1" --rpc-url "$RPC" | num; }
field(){ cast call "$VAULT" "$OFFRAMPS_GETTER" "$1" --rpc-url "$RPC" | sed -n "${2}p" | num; }   # 6=status 4=lockedAt 7=qrHash
tx() { # tx <label> <to> <sig> args...
  local label="$1" to="$2"; shift 2; local out
  out="$(cast send "$to" "$@" --private-key "$PK" --rpc-url "$RPC" "${GASFLAGS[@]}" --json 2>&1)" \
    || { bad "$label FAILED: $(tail -2 <<<"$out")"; say "=== aborted: $PASS passed, $FAIL failed ==="; exit 1; }
  local h g p s; h="$(jq -r .transactionHash <<<"$out")"; g="$(jq -r .gasUsed <<<"$out")"; p="$(jq -r .effectiveGasPrice <<<"$out")"; s="$(jq -r .status <<<"$out")"
  GAS_WEI=$(( GAS_WEI + g * p )); NTX=$((NTX+1)); LAST_TX="$h"
  [ "$s" = "0x1" ] || { bad "$label reverted: $EXPL/tx/$h"; exit 1; }
  say "  ↳ $label  gas=$((g))  $EXPL/tx/$h"
}
send() { local label="$1"; shift; tx "$label" "$VAULT" "$@"; }
expect_revert() { # expect_revert <label> <errorSig> <from> <fnSig> args...
  local label="$1" err="$2" from="$3"; shift 3; local out
  if out="$(cast call "$VAULT" "$@" --from "$from" --rpc-url "$RPC" 2>&1)"; then bad "$label: expected ${err%%(*}, but the call succeeded"; return; fi
  local sel; sel="$(cast sig "$err")"
  if grep -qi "${sel#0x}" <<<"$out"; then ok "$label → reverts with ${err%%(*}"; else bad "$label reverted, but not with ${err%%(*}: $(tail -1 <<<"$out")"; fi
}
invariant() {
  local b e p; b="$(bal "$VAULT")"; e="$(vget 'escrowedTotal()(uint256)')"; p="$(vget 'poolBalance()(uint256)')"
  check "invariant: vault balance $b ≥ escrow $e + pool $p" [ "$b" -ge $((e+p)) ]
}
sign_permit() { # <value> <deadline> → PV PR PS   (EIP-2612 typed data, as MetaMask would sign it)
  local nonce json sig; nonce="$(cast call "$TOKEN" 'nonces(address)(uint256)' "$ME" --rpc-url "$RPC" | num)"
  json=$(jq -nc --arg n "$TNAME" --arg v "$TVER" --argjson c "$CHAIN_ID" --arg t "$TOKEN" --arg o "$ME" --arg s "$VAULT" \
    --arg val "$1" --arg nonce "$nonce" --arg d "$2" '{
    types:{EIP712Domain:[{name:"name",type:"string"},{name:"version",type:"string"},{name:"chainId",type:"uint256"},{name:"verifyingContract",type:"address"}],
           Permit:[{name:"owner",type:"address"},{name:"spender",type:"address"},{name:"value",type:"uint256"},{name:"nonce",type:"uint256"},{name:"deadline",type:"uint256"}]},
    primaryType:"Permit", domain:{name:$n,version:$v,chainId:$c,verifyingContract:$t},
    message:{owner:$o,spender:$s,value:$val,nonce:$nonce,deadline:$d}}')
  sig="$(cast wallet sign --data "$json" --private-key "$PK")"
  PR="0x${sig:2:64}"; PS="0x${sig:66:64}"; PV="$((16#${sig:130:2}))"
}
sign_intent() { # <id> <qr> <deadline> → ISIG   (our OfframpIntent typed data)
  local json; json=$(jq -nc --argjson c "$CHAIN_ID" --arg vault "$VAULT" --arg id "$1" --arg u "$ME" --arg a "$AMT" \
    --arg inr "$INR" --arg r "$RATE" --arg q "$2" --arg d "$3" '{
    types:{EIP712Domain:[{name:"name",type:"string"},{name:"version",type:"string"},{name:"chainId",type:"uint256"},{name:"verifyingContract",type:"address"}],
           OfframpIntent:[{name:"id",type:"bytes32"},{name:"user",type:"address"},{name:"amount",type:"uint256"},{name:"inrPaise",type:"uint64"},{name:"rateE6",type:"uint64"},{name:"qrText",type:"string"},{name:"deadline",type:"uint64"}]},
    primaryType:"OfframpIntent", domain:{name:"ScanPayVault",version:"1",chainId:$c,verifyingContract:$vault},
    message:{id:$id,user:$u,amount:$a,inrPaise:$inr,rateE6:$r,qrText:$q,deadline:$d}}')
  ISIG="$(cast wallet sign --data "$json" --private-key "$PK")"
}
intent_tuple() { printf '(%s,%s,%s,%s,%s,"%s",%s)' "$1" "$ME" "$AMT" "$INR" "$RATE" "$2" "$3"; }
do_offramp() { # <label> <qr> → ID, LAST_INTENT, LAST_ISIG, LAST_PERMIT
  ID="0x$(openssl rand -hex 32)"; local dl=$(( $(date +%s) + 300 ))
  sign_permit "$AMT" "$dl"; sign_intent "$ID" "$2" "$dl"
  LAST_INTENT="$(intent_tuple "$ID" "$2" "$dl")"; LAST_ISIG="$ISIG"; LAST_PERMIT="($dl,$PV,$PR,$PS)"
  send "$1" "$OFFRAMP_SIG" "$LAST_INTENT" "$LAST_ISIG" "$LAST_PERMIT"
}

say "=== ScanPayVault live smoke test · $CHAIN (chain $CHAIN_ID) · $(date -u +%FT%TZ) ==="
say "vault  $EXPL/address/$VAULT"
say "token  $TOKEN ($TNAME, permit domain v$TVER) · wallet $ME"
START="$(bal "$ME")"; NATIVE_START="$(cast balance "$ME" --ether --rpc-url "$RPC")"
say "start: wallet token $START (base units) · native $NATIVE_START"

say "[0] deployment config"
check "owner = wallet"   [ "$(cast call "$VAULT" 'owner()(address)' --rpc-url "$RPC")" = "$ME" ]
check "settler = wallet" [ "$(cast call "$VAULT" 'settler()(address)' --rpc-url "$RPC")" = "$ME" ]
check "token = $EXP_TOKEN" [ "$TOKEN" = "$EXP_TOKEN" ]
check "decimals = 6, completeDelay = 15 s" [ "$(vget 'decimals()(uint8)')/$(vget 'completeDelay()(uint256)')" = "6/15" ]
send "setLimits(max 11 tokens, 20 tokens/day)" 'setLimits(uint256,uint256)' 11000000 20000000
check "limits applied" [ "$(vget 'maxOfframpAmount()(uint256)')/$(vget 'dailyEmitCap()(uint256)')" = "11000000/20000000" ]
expect_revert "setLimits by a stranger" 'OwnableUnauthorizedAccount(address)' 0x000000000000000000000000000000000000dEaD 'setLimits(uint256,uint256)' 1 1

say "[1] offramp A (valid UPI QR), gasless-style permit + intent → LOCKED"
W0="$(wbal)"; do_offramp "offramp A" "$QR_OK"; A_ID="$ID"; A_TX="$LAST_TX"
A_INTENT="$LAST_INTENT"; A_ISIG="$LAST_ISIG"; A_PERMIT="$LAST_PERMIT"
check "status = LOCKED" [ "$(field "$A_ID" 6)" = 1 ]
check "wallet debited exactly 0.1 token" near $(( W0 - $(wbal) )) "$AMT"
EV="$(cast receipt "$A_TX" --json --rpc-url "$RPC" | jq -r --arg v "$VAULT_LC" '[.logs[] | select((.address|ascii_downcase)==$v)][0].data')"
QR_ONCHAIN="$(cast abi-decode 'f()(uint256,uint64,uint64,string)' "$EV" | sed -n 4p | sed 's/^"//; s/"$//')"
check "OfframpLocked event carries the exact raw qrText" [ "$QR_ONCHAIN" = "$QR_OK" ]
check "stored qrHash = keccak256(qrText)" [ "$(field "$A_ID" 7)" = "$(cast keccak "$QR_OK")" ]
expect_revert "replay the same signed intent" 'OfframpIdUsed(bytes32)' "$ME" "$OFFRAMP_SIG" "$A_INTENT" "$A_ISIG" "$A_PERMIT"
invariant

say "[2] completeOfframp before 15 s must fail"
expect_revert "complete at t < 15 s" 'TooEarly(uint256)' "$ME" 'completeOfframp(bytes32,bytes32)' "$A_ID" "$ZERO32"
expect_revert "complete by a stranger" 'UnauthorizedSettler(address)' 0x000000000000000000000000000000000000dEaD 'completeOfframp(bytes32,bytes32)' "$A_ID" "$ZERO32"
expect_revert "reclaim before 24 h" 'TooEarly(uint256)' "$ME" 'reclaimOfframp(bytes32)' "$A_ID"

say "[3] wait for lockedAt + 15 s, then complete → COMPLETED"
LOCKED_AT="$(field "$A_ID" 4)"
while [ "$(cast block latest -f timestamp --rpc-url "$RPC")" -lt $((LOCKED_AT + 15)) ]; do sleep 2; done
P0="$(vget 'poolBalance()(uint256)')"
send "completeOfframp A" 'completeOfframp(bytes32,bytes32)' "$A_ID" "$ZERO32"
check "status = COMPLETED" [ "$(field "$A_ID" 6)" = 2 ]
check "escrow moved to pool (+0.1)" [ $(( $(vget 'poolBalance()(uint256)') - P0 )) = "$AMT" ]
invariant

say "[4] reverseOfframp A within 24 h → REVERSED, paid back to the original user"
W1="$(wbal)"; send "reverseOfframp A" 'reverseOfframp(bytes32)' "$A_ID"
check "status = REVERSED" [ "$(field "$A_ID" 6)" = 4 ]
check "original user got 0.1 back" near $(( $(wbal) - W1 )) "$AMT"
expect_revert "reverse twice" 'InvalidStatus(bytes32,uint8,uint8)' "$ME" 'reverseOfframp(bytes32)' "$A_ID"
invariant

say "[5] offramp B (bad QR) → refundOfframp(INVALID_QR) → REFUNDED"
W2="$(wbal)"; do_offramp "offramp B" "$QR_BAD"; B_ID="$ID"
send "refundOfframp B, reason 1 INVALID_QR" 'refundOfframp(bytes32,uint8)' "$B_ID" 1
check "status = REFUNDED" [ "$(field "$B_ID" 6)" = 3 ]
check "user balance fully restored" near "$(wbal)" "$W2"
expect_revert "complete after refund" 'InvalidStatus(bytes32,uint8,uint8)' "$ME" 'completeOfframp(bytes32,bytes32)' "$B_ID" "$ZERO32"
invariant

say "[6] pool: fundPool → onramp → emitPayment → withdrawPool"
tx "approve vault 0.2" "$TOKEN" 'approve(address,uint256)' "$VAULT" 200000
send "fundPool 0.2" 'fundPool(uint256)' 200000
ORDER="0x$(openssl rand -hex 32)"; W3="$(wbal)"
send "onramp 0.1 for ₹10" 'onramp(bytes32,address,uint256,uint64)' "$ORDER" "$ME" "$AMT" "$INR"
check "onramp paid 0.1 to the wallet" near $(( $(wbal) - W3 )) "$AMT"
expect_revert "onramp with the same orderId" 'OnrampAlreadyPaid(bytes32)' "$ME" 'onramp(bytes32,address,uint256,uint64)' "$ORDER" "$ME" "$AMT" "$INR"
expect_revert "onramp larger than the pool" 'InsufficientPoolBalance(uint256,uint256)' "$ME" 'onramp(bytes32,address,uint256,uint64)' "0x$(openssl rand -hex 32)" "$ME" 5000000 "$INR"
send "emitPayment 0.05" 'emitPayment(address,uint256)' "$ME" 50000
check "daily cap used: 0.15 of 20" [ "$(vget 'remainingDailyCap()(uint256)')" = 19850000 ]
expect_revert "withdrawPool beyond pool (escrow untouchable)" 'InsufficientPoolBalance(uint256,uint256)' "$ME" 'withdrawPool(address,uint256)' "$ME" 999999999
POOL="$(vget 'poolBalance()(uint256)')"; send "withdrawPool remaining $POOL" 'withdrawPool(address,uint256)' "$ME" "$POOL"
check "pool = 0 and escrow = 0" [ "$(vget 'poolBalance()(uint256)')/$(vget 'escrowedTotal()(uint256)')" = "0/0" ]
invariant

END="$(bal "$ME")"; ADJ="$(wbal)"
say "=== end: wallet token $START → $END · $NTX txs · gas $(cast from-wei "$GAS_WEI") native ==="
check "token-neutral: every token came back (gas aside)" near "$ADJ" "$START"
say "=== RESULT: $PASS passed, $FAIL failed · log: $LOG ==="
[ "$FAIL" = 0 ]
