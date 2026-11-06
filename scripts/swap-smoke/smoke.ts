/**
 * Live smoke test of the PWA's "Swap EURC → USDC" path on Arc testnet. IT SENDS REAL TESTNET TRANSACTIONS.
 *
 * It runs the app's own code (apps/web/src/lib/swap.ts: sizing, safety checks, createArcSwapper) through a
 * headless EIP-1193 wallet for OTHER_WALLET_PRIVATE_KEY. That key is read as text from the repo-root .env inside
 * this process, format-checked, and never printed. Only public addresses, amounts and tx links are printed.
 *
 *   npx tsx smoke.ts balances
 *   npx tsx smoke.ts seed-eurc 2.00 --yes     setup only: swap USDC → EURC so the wallet has EURC to sell
 *   npx tsx smoke.ts park 0.60 --yes          setup only: send USDC above 0.60 to the deployer, so a payment needs a swap
 *   npx tsx smoke.ts swap --inr 100 [--yes]   the app path: quote → shortfall → size → (confirm) → swap → re-read
 *   npx tsx smoke.ts unwind --yes             cleanup: swap the whole EURC balance back to USDC
 *   npx tsx smoke.ts unpark 17.80 --yes       cleanup: send parked USDC from the deployer (SEED_PHRASE index 0) back
 *
 * SMOKE_DEBUG=1 logs every wallet/RPC method and the full error chain.
 *
 * Without --yes, the swap command stops after sizing (estimates only, nothing sent).
 * The backend must be running (API env var, default http://127.0.0.1:8788).
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createPublicClient, createWalletClient, http, parseAbi, toHex, type Hex } from "viem";
import { mnemonicToAccount, privateKeyToAccount, type HDAccount, type PrivateKeyAccount } from "viem/accounts";
import { arcTestnet } from "viem/chains";
import {
  assertEstimateSafe, createArcSwapper, EURC_ADDRESS, gasFeeText, gasFeeUnits, minOutOf, readEurcBalance, requiredOut,
  safeExplorerUrl, shouldOfferSwap, sizeSwap, unitsFromDecimal, unitsToDecimal,
} from "../../apps/web/src/lib/swap";
import type { Eip1193Provider } from "../../apps/web/src/lib/wallet";

const ENV_FILE = fileURLToPath(new URL("../../.env", import.meta.url));
const API = process.env.API ?? "http://127.0.0.1:8788";
const RPC = "https://rpc.testnet.arc.io";
const USDC = "0x3600000000000000000000000000000000000000";
const DEPLOYER = "0xEe5658e3479BB162B9a0D45F03e3ac0066A00BC1";
const DEFAULT_QR = "upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point&cu=INR";
const ERC20 = parseAbi([
  "function balanceOf(address) view returns (uint256)",
  "function transfer(address to, uint256 amount) returns (bool)",
]);

const log = (...parts: unknown[]) => console.log(...parts);
const DEBUG = process.env.SMOKE_DEBUG === "1";

/** Reads one key from .env as text. Errors name the variable, never the value. */
function loadUserAccount(): PrivateKeyAccount {
  const name = "OTHER_WALLET_PRIVATE_KEY";
  const line = readFileSync(ENV_FILE, "utf8").split(/\r?\n/).find((l) => l.startsWith(`${name}=`));
  if (!line) throw new Error(`${name} is missing from .env`);
  let value = line.slice(name.length + 1).trim().replace(/^(['"])(.*)\1$/, "$2");
  if (!/^(0x)?[0-9a-fA-F]{64}$/.test(value)) throw new Error(`${name} is not a 32-byte hex key (value not shown)`);
  if (!value.startsWith("0x")) value = `0x${value}`;
  return privateKeyToAccount(value as Hex);
}

/** A minimal browser-wallet stand-in: signs and sends with the local account, passes reads to the RPC. */
function headlessWallet(account: PrivateKeyAccount): Eip1193Provider {
  const pub = createPublicClient({ chain: arcTestnet, transport: http(RPC) });
  const wallet = createWalletClient({ account, chain: arcTestnet, transport: http(RPC) });
  const big = (v: unknown) => (v === undefined || v === null ? undefined : BigInt(v as string));
  return {
    async request({ method, params = [] }: { method: string; params?: unknown[] }) {
      switch (method) {
        case "eth_accounts":
        case "eth_requestAccounts":
          return [account.address];
        case "eth_chainId":
          return toHex(arcTestnet.id);
        case "wallet_switchEthereumChain":
        case "wallet_addEthereumChain":
          return null;
        case "eth_sendTransaction": {
          const tx = params[0] as Record<string, string | undefined>;
          log(`  wallet: sendTransaction to ${tx.to} (${(tx.data?.length ?? 2) / 2 - 1} bytes of calldata)`);
          const hash = await wallet.sendTransaction({
            to: tx.to as Hex, data: tx.data as Hex | undefined, value: big(tx.value), gas: big(tx.gas),
            maxFeePerGas: big(tx.maxFeePerGas), maxPriorityFeePerGas: big(tx.maxPriorityFeePerGas),
          });
          log(`  wallet: sent ${arcTestnet.blockExplorers.default.url}/tx/${hash}`);
          return hash;
        }
        case "eth_signTypedData_v4": {
          const typed = typeof params[1] === "string" ? JSON.parse(params[1]) : params[1];
          const { EIP712Domain: _domain, ...types } = typed.types;
          log(`  wallet: signTypedData ${typed.primaryType}`);
          return account.signTypedData({ domain: typed.domain, types, primaryType: typed.primaryType, message: typed.message });
        }
        case "personal_sign":
          return account.signMessage({ message: { raw: params[0] as Hex } });
        default:
          if (DEBUG) log(`  rpc: ${method} ${JSON.stringify(params).slice(0, 160)}`);
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          return pub.request({ method, params } as any).catch((e: unknown) => {
            if (DEBUG) log(`  rpc: ${method} FAILED ${(e as Error).message.split("\n")[0]}`);
            throw e;
          });
      }
    },
  };
}

async function api<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(API + path, body === undefined ? undefined : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status} ${JSON.stringify(json)}`);
  return json as T;
}

async function usdcBalance(address: Hex): Promise<bigint> {
  const pub = createPublicClient({ chain: arcTestnet, transport: http(RPC) });
  return pub.readContract({ address: USDC, abi: ERC20, functionName: "balanceOf", args: [address] });
}

async function balances(address: Hex) {
  const [usdc, eurc] = await Promise.all([usdcBalance(address), readEurcBalance(RPC, address)]);
  log(`balances ${address}: ${unitsToDecimal(usdc)} USDC · ${unitsToDecimal(eurc)} EURC`);
  return { usdc, eurc };
}

/** Setup and cleanup only (seed-eurc, unwind): a plain same-chain swap with the same Swap Kit and adapter. */
async function directSwap(provider: Eip1193Provider, address: Hex, tokenIn: "USDC" | "EURC", tokenOut: "USDC" | "EURC", amount: string, yes: boolean) {
  const [{ SwapKit }, { createViemAdapterFromProvider }] = await Promise.all([
    import("@circle-fin/swap-kit"),
    import("@circle-fin/adapter-viem-v2"),
  ]);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const adapter = await createViemAdapterFromProvider({ provider: provider as any });
  const kit = new SwapKit();
  const params = { from: { adapter, chain: "Arc_Testnet" }, tokenIn, tokenOut, amountIn: amount, config: { slippageBps: 300 } };
  const est = await kit.estimate(params);
  log(`estimate: ${amount} ${tokenIn} → ≥ ${est.stopLimit.amount} ${tokenOut} (expected ${est.estimatedOutput?.amount}), toAddress ${est.toAddress}`);
  if (String(est.toAddress).toLowerCase() !== address.toLowerCase()) throw new Error("estimate pays a different address; aborting");
  if (!yes) return log("dry run: pass --yes to send");
  const res = await kit.swap(params);
  log(`swap: ${res.amountIn} ${tokenIn} → ${res.amountOut} ${tokenOut} · status ${res.progress?.status} · ${res.explorerUrl ?? res.txHash}`);
  if (res.progress?.status !== "DONE") throw new Error(`swap status ${res.progress?.status}`);
}

/** Cleanup only: swap the wallet's whole EURC balance back to USDC. */
async function unwind(provider: Eip1193Provider, address: Hex, yes: boolean) {
  const eurc = await readEurcBalance(RPC, address);
  if (eurc === 0n) return log("no EURC to unwind");
  await directSwap(provider, address, "EURC", "USDC", unitsToDecimal(eurc), yes);
}

/** The deployer key from SEED_PHRASE (index 0), read as text inside this process. Errors never include the value. */
function loadDeployerAccount(): PrivateKeyAccount | HDAccount {
  const name = "SEED_PHRASE";
  const line = readFileSync(ENV_FILE, "utf8").split(/\r?\n/).find((l) => l.startsWith(`${name}=`));
  if (!line) throw new Error(`${name} is missing from .env`);
  const phrase = line.slice(name.length + 1).trim().replace(/^(['"])(.*)\1$/, "$2");
  const words = phrase.split(/\s+/).filter(Boolean).length;
  if (words !== 12 && words !== 24) throw new Error(`${name} is not a 12- or 24-word phrase (value not shown)`);
  let account: HDAccount;
  try {
    account = mnemonicToAccount(phrase, { addressIndex: 0 });
  } catch {
    throw new Error(`${name} could not be read as a mnemonic (value not shown)`);
  }
  if (account.address.toLowerCase() !== DEPLOYER.toLowerCase()) throw new Error(`${name} index 0 is not the deployer; aborting`);
  return account;
}

/** Cleanup only: send parked USDC from the deployer back to the test wallet. */
async function unpark(to: Hex, amount: string, yes: boolean) {
  const deployer = loadDeployerAccount();
  const units = unitsFromDecimal(amount);
  log(`unpark: send ${unitsToDecimal(units)} USDC from the deployer ${deployer.address} to ${to}`);
  if (!yes) return log("dry run: pass --yes to send");
  const wallet = createWalletClient({ account: deployer, chain: arcTestnet, transport: http(RPC) });
  const pub = createPublicClient({ chain: arcTestnet, transport: http(RPC) });
  const hash = await wallet.writeContract({ address: USDC, abi: ERC20, functionName: "transfer", args: [to, units] });
  await pub.waitForTransactionReceipt({ hash });
  log(`unpark: ${arcTestnet.blockExplorers.default.url}/tx/${hash}`);
}

/** Setup only: lower the USDC balance so the payment needs a swap. The USDC goes to our own deployer wallet. */
async function park(account: PrivateKeyAccount, keep: string, yes: boolean) {
  const bal = await usdcBalance(account.address);
  const keepUnits = unitsFromDecimal(keep);
  if (bal <= keepUnits) return log(`balance ${unitsToDecimal(bal)} USDC is already ≤ ${keep}; nothing to park`);
  const amount = bal - keepUnits;
  log(`park: send ${unitsToDecimal(amount)} USDC to the deployer ${DEPLOYER}, keeping ${keep} USDC`);
  if (!yes) return log("dry run: pass --yes to send");
  const wallet = createWalletClient({ account, chain: arcTestnet, transport: http(RPC) });
  const pub = createPublicClient({ chain: arcTestnet, transport: http(RPC) });
  const hash = await wallet.writeContract({ address: USDC, abi: ERC20, functionName: "transfer", args: [DEPLOYER, amount] });
  await pub.waitForTransactionReceipt({ hash });
  log(`park: ${arcTestnet.blockExplorers.default.url}/tx/${hash}`);
}

/** The app path, step for step as SwapCard.prepare() and SwapCard.confirm() run it. */
async function swapForPayment(provider: Eip1193Provider, address: Hex, inr: string, yes: boolean) {
  type Cfg = { chains: Record<string, { chainId: number; rpc: string; explorer: string }> };
  const cfg = await api<Cfg>("/config");
  const chain = cfg.chains.arc;
  const account = await api<{ balance: string; balanceFormatted: string }>(`/account/arc/${address}`);
  const quote = await api<{ id: string; intent: { amount: string }; display: { amount: string } }>(
    "/quote", { chain: "arc", user: address, inr, qrText: DEFAULT_QR },
  );
  const balance = BigInt(account.balance);
  const required = BigInt(quote.intent.amount);
  log(`quote ₹${inr}: needs ${quote.display.amount} USDC · wallet has ${account.balanceFormatted} USDC`);
  if (!shouldOfferSwap(chain, balance < required)) return log("no swap needed: the app would not show the swap card");
  log(`swap card shown: shortfall ${unitsToDecimal(required - balance)} USDC`);

  const eurcBalance = await readEurcBalance(chain.rpc, address);
  const kit = await createArcSwapper(provider, {
    onPrompt: () => log("  ui: Confirm the swap in your wallet…"),
    onSent: () => log("  ui: Swapping…"),
  });
  const sized = await sizeSwap({
    shortfall: required - balance,
    eurcBalance,
    estimate: async (amountIn) => {
      const estimate = await kit.estimate(amountIn);
      assertEstimateSafe(estimate, address);
      const minOut = minOutOf(estimate);
      const gasFee = gasFeeUnits(estimate);
      log(`  estimate ${unitsToDecimal(amountIn)} EURC → ≥ ${unitsToDecimal(minOut)} USDC (expected ${estimate.estimatedOutput?.amount}) · ` +
        `needs ≥ ${unitsToDecimal(requiredOut(required - balance, gasFee))} incl. gas`);
      return { estimate, minOut, gasFee };
    },
  });
  log(`dialog: Swap ${unitsToDecimal(sized.amountIn)} EURC → at least ${unitsToDecimal(sized.quote.minOut)} USDC · ` +
    `network fee ${gasFeeText(sized.quote.estimate) ?? "shown by your wallet"} · ${sized.estimates} estimate call(s)`);
  if (!yes) return log("dry run: pass --yes to press Swap");

  assertEstimateSafe(sized.quote.estimate, address);
  const result = await kit.swap(sized.amountIn);
  log(`swap result: ${result.amountIn} EURC → ${result.amountOut} USDC · status ${result.progress?.status}` +
    ` · ${safeExplorerUrl(result, chain.explorer) ?? result.txHash}`);
  if (result.progress?.status === "FAILED" || result.progress?.status === "NOT_FOUND") throw new Error("swap failed");

  const after = await api<{ balance: string; balanceFormatted: string }>(`/account/arc/${address}`);
  const fresh = await api<{ intent: { amount: string }; display: { amount: string } }>(
    "/quote", { chain: "arc", user: address, inr, qrText: DEFAULT_QR },
  );
  const ok = BigInt(after.balance) >= BigInt(fresh.intent.amount);
  log(`after swap: wallet ${after.balanceFormatted} USDC · fresh quote needs ${fresh.display.amount} USDC → Pay ${ok ? "ENABLED" : "STILL DISABLED"}`);
  if (!ok) process.exitCode = 3;
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const yes = rest.includes("--yes");
  const arg = (flag: string) => { const i = rest.indexOf(flag); return i >= 0 ? rest[i + 1] : undefined; };
  const account = loadUserAccount();
  const provider = headlessWallet(account);
  log(`user ${account.address} · EURC ${EURC_ADDRESS} · api ${API}`);
  switch (cmd) {
    case "balances": await balances(account.address); break;
    case "seed-eurc": await directSwap(provider, account.address, "USDC", "EURC", rest[0] ?? "2.00", yes); await balances(account.address); break;
    case "unwind": await unwind(provider, account.address, yes); await balances(account.address); break;
    case "unpark": await unpark(account.address, rest[0] ?? "0", yes); await balances(account.address); break;
    case "park": await park(account, rest[0] ?? "0.60", yes); await balances(account.address); break;
    case "swap": await swapForPayment(provider, account.address, arg("--inr") ?? "100", yes); await balances(account.address); break;
    default: throw new Error("usage: smoke.ts balances | seed-eurc <usdc> | park <keepUsdc> | swap --inr <n> | unwind | unpark <usdc>  [--yes]");
  }
}

main().catch((e) => {
  // Error text from viem/the SDK never contains the key: it is only ever passed to privateKeyToAccount above.
  console.error("FAILED:", e instanceof Error ? e.message.split("\n")[0] : e);
  if (DEBUG) {
    for (let c = (e as { cause?: unknown })?.cause, i = 0; c && i < 5; c = (c as { cause?: unknown }).cause, i++) {
      console.error(`  cause ${i}:`, c instanceof Error ? c.message.split("\n").slice(0, 3).join(" | ") : JSON.stringify(c).slice(0, 300));
    }
    const ctx = (e as { context?: unknown; code?: unknown })?.context ?? (e as { details?: unknown })?.details;
    if (ctx) console.error("  context:", JSON.stringify(ctx).slice(0, 600));
  }
  process.exit(1);
});
