/**
 * "Swap EURC to USDC" on Arc testnet, with Circle's Swap Kit in the user's own wallet.
 *
 * The sizing, safety and error helpers are pure, so they can be unit-tested. The Circle packages are loaded
 * with dynamic `import()` only when the user presses the swap button, so they stay out of the main bundle
 * and the PWA precache.
 */

import type { SwapEstimate, SwapParams, SwapResult } from "@circle-fin/swap-kit";
import type { EIP1193Provider } from "viem";
import { formatUnits } from "./format";
import { isUserRejection, walletErrorMessage, type Eip1193Provider } from "./wallet";

export const ARC_CHAIN_ID = 5042002;
/** Swap Kit's name for Arc testnet. */
export const ARC_SWAP_CHAIN = "Arc_Testnet";
/** Arc testnet EURC (ERC-20 interface, 6 decimals). */
export const EURC_ADDRESS = "0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a";
export const SWAP_DECIMALS = 6;
/** Testnet liquidity is thin, so the default 3% tolerance is kept. */
export const SLIPPAGE_BPS = 300;

/** 3% buffer plus 0.05 USDC for the network fee and quote drift. */
const BUFFER_NUMERATOR = 103n;
const BUFFER_DENOMINATOR = 100n;
const BUFFER_EXTRA = 50_000n;
const BUMP_NUMERATOR = 102n;
const BUMP_DENOMINATOR = 100n;
export const MAX_SIZING_TRIES = 3;
/** Gas set aside when an estimate doesn't report its network fee in USDC: 0.05 USDC. */
export const GAS_RESERVE = 50_000n;

/** The swap card is for the Arc vault only. Circle Swap doesn't cover Whitechain. */
export function shouldOfferSwap(chain: { chainId: number }, tooLow: boolean): boolean {
  return tooLow && chain.chainId === ARC_CHAIN_ID;
}

export const MSG_NOT_ENOUGH_EURC = "Not enough EURC to cover this payment either.";
export const MSG_NO_QUOTE = "We couldn't get a good swap rate right now. Try again in a moment.";
export const MSG_LIQUIDITY = "The swap pool is short of liquidity right now. Try a smaller amount or try again.";
export const MSG_GAS = "You need a little USDC on Arc to pay the network fee for the swap.";
export const MSG_GENERIC = "The swap couldn't be completed. Please try again.";

// ---------------------------------------------------------------------------------------------------------------
// Amount maths (bigint, 6-decimal base units)
// ---------------------------------------------------------------------------------------------------------------

export const ceilDiv = (a: bigint, b: bigint): bigint => (a + b - 1n) / b;
const minBig = (a: bigint, b: bigint): bigint => (a < b ? a : b);
const maxBig = (a: bigint, b: bigint): bigint => (a > b ? a : b);

/** Reads a decimal string such as "1.5" into base units. Extra digits are dropped, or rounded up with `ceil`. */
export function unitsFromDecimal(value: string, decimals = SWAP_DECIMALS, mode: "floor" | "ceil" = "floor"): bigint {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) throw new Error(`Not a decimal amount: ${value}`);
  const fraction = match[2] ?? "";
  const kept = fraction.slice(0, decimals).padEnd(decimals, "0");
  const base = BigInt(match[1] + kept);
  const dropped = fraction.slice(decimals);
  return mode === "ceil" && /[1-9]/.test(dropped) ? base + 1n : base;
}

/** Base units to the decimal string Swap Kit expects, for example 1_500_000n to "1.5". */
export function unitsToDecimal(units: bigint, decimals = SWAP_DECIMALS): string {
  return formatUnits(units.toString(), decimals);
}

/** The USDC to aim for: the shortfall plus the buffer. */
export function swapTarget(shortfall: bigint): bigint {
  return ceilDiv(shortfall * BUFFER_NUMERATOR, BUFFER_DENOMINATOR) + BUFFER_EXTRA;
}

/** The next, larger try: 2% more. */
export function bumpAmount(amountIn: bigint): bigint {
  return ceilDiv(amountIn * BUMP_NUMERATOR, BUMP_DENOMINATOR);
}

/** The smallest amount of USDC the swap can return (the estimate's stop limit), in base units. */
export function minOutOf(estimate: Pick<SwapEstimate, "stopLimit">): bigint {
  return unitsFromDecimal(estimate.stopLimit.amount, SWAP_DECIMALS, "floor");
}

/**
 * The swap's network fee in USDC base units, rounded up. Null when the estimate has no gas entry, or reports
 * it in another token, so the caller falls back to GAS_RESERVE.
 */
export function gasFeeUnits(estimate: Pick<SwapEstimate, "fees">): bigint | null {
  const fee = estimate.fees?.find((f) => f.type === "gas");
  if (!fee?.amount || String(fee.token).toUpperCase() !== "USDC") return null;
  try {
    return unitsFromDecimal(fee.amount, SWAP_DECIMALS, "ceil");
  } catch {
    return null;
  }
}

export type SizingErrorCode = "NOT_ENOUGH_EURC" | "NO_QUOTE" | "NO_SHORTFALL";

export class SwapSizingError extends Error {
  constructor(public code: SizingErrorCode) {
    super(code === "NOT_ENOUGH_EURC" ? MSG_NOT_ENOUGH_EURC : code === "NO_QUOTE" ? MSG_NO_QUOTE : "Nothing to swap.");
  }
}

export interface SizedSwap<T> {
  /** EURC to sell, in base units. */
  amountIn: bigint;
  /** What `estimate` returned for `amountIn`. */
  quote: T;
  /** Estimate calls used, including the first probe. */
  estimates: number;
}

/** What a sized swap must return at least: the shortfall plus the swap's own gas, which comes out of the same USDC. */
export function requiredOut(shortfall: bigint, gasFee: bigint | null | undefined): bigint {
  return shortfall + (gasFee ?? GAS_RESERVE);
}

/**
 * Works out how much EURC to sell.
 *
 * The swap's network fee is paid in USDC from the same balance the payment uses, so after the swap the wallet holds
 * `balance + out - gasFee`. A swap is therefore accepted only when its minimum output covers `shortfall + gasFee`
 * (the estimate's USDC gas fee, or GAS_RESERVE when it reports none).
 *
 *  1. Aim for `targetOut = max(shortfall x 1.03 + 0.05 USDC, shortfall + gasFee)`.
 *  2. Probe once, take the rate from the MINIMUM output (the stop limit), and size `amountIn` from it.
 *  3. Re-estimate. Accept only if the minimum output covers `shortfall + gasFee`. Otherwise add 2% and retry,
 *     up to 3 tries.
 *  4. `amountIn` never exceeds the EURC balance. If even the whole balance can't cover it, stop.
 */
export async function sizeSwap<T extends { minOut: bigint; gasFee?: bigint | null }>(opts: {
  shortfall: bigint;
  eurcBalance: bigint;
  estimate: (amountIn: bigint) => Promise<T>;
}): Promise<SizedSwap<T>> {
  const { shortfall, eurcBalance, estimate } = opts;
  if (shortfall <= 0n) throw new SwapSizingError("NO_SHORTFALL");
  if (eurcBalance <= 0n) throw new SwapSizingError("NOT_ENOUGH_EURC");

  // EURC and USDC are close to 1:1, so the buffered target is a fair first guess for the probe.
  const probeIn = minBig(swapTarget(shortfall), eurcBalance);
  const probe = await estimate(probeIn);
  let estimates = 1;
  if (probe.minOut <= 0n) throw new SwapSizingError("NO_QUOTE");
  const targetOut = maxBig(swapTarget(shortfall), requiredOut(shortfall, probe.gasFee));

  // rate = minOut / probeIn, so amountIn = targetOut / rate, rounded up.
  let amountIn = minBig(ceilDiv(targetOut * probeIn, probe.minOut), eurcBalance);
  for (let attempt = 1; attempt <= MAX_SIZING_TRIES; attempt++) {
    const quote = amountIn === probeIn ? probe : await estimate(amountIn);
    if (amountIn !== probeIn) estimates++;
    if (quote.minOut >= requiredOut(shortfall, quote.gasFee)) return { amountIn, quote, estimates };
    if (amountIn >= eurcBalance) throw new SwapSizingError("NOT_ENOUGH_EURC");
    amountIn = minBig(bumpAmount(amountIn), eurcBalance);
  }
  throw new SwapSizingError("NO_QUOTE");
}

// ---------------------------------------------------------------------------------------------------------------
// Safety checks and messages
// ---------------------------------------------------------------------------------------------------------------

export class SwapSafetyError extends Error {}

/** The estimate must be for exactly the swap that was asked for, from and to the connected wallet. */
export function assertEstimateSafe(
  estimate: Pick<SwapEstimate, "tokenIn" | "tokenOut" | "chainIn" | "chainOut" | "fromAddress" | "toAddress">,
  address: string,
): void {
  const same = (a: string | undefined) => !a || a.toLowerCase() === address.toLowerCase();
  if (String(estimate.tokenIn).toUpperCase() !== "EURC" || String(estimate.tokenOut).toUpperCase() !== "USDC") {
    throw new SwapSafetyError("The swap quote is for different tokens. Nothing was sent.");
  }
  if (String(estimate.chainIn) !== ARC_SWAP_CHAIN || String(estimate.chainOut) !== ARC_SWAP_CHAIN) {
    throw new SwapSafetyError("The swap quote is for a different network. Nothing was sent.");
  }
  if (!same(estimate.toAddress) || !same(estimate.fromAddress)) {
    throw new SwapSafetyError("The swap quote is for a different wallet address. Nothing was sent.");
  }
}

/** The network fee, as text, from the estimate's gas fee. Null when the service didn't give one. */
export function gasFeeText(estimate: Pick<SwapEstimate, "fees">): string | null {
  const fee = estimate.fees?.find((f) => f.type === "gas" && f.amount !== null && f.amount !== "");
  if (!fee?.amount) return null;
  // The service reports gas with many decimals (0.017489471475). Show it to 6 places, rounded up.
  let shown = fee.amount;
  try {
    shown = unitsToDecimal(unitsFromDecimal(fee.amount, SWAP_DECIMALS, "ceil"));
  } catch {
    // keep the raw text
  }
  return `${shown} ${fee.token}`;
}

function errorText(error: unknown): string {
  const e = error as { message?: unknown; shortMessage?: unknown; cause?: unknown } | null;
  const cause = e?.cause as { message?: unknown } | undefined;
  return [e?.message, e?.shortMessage, cause?.message].filter((part) => typeof part === "string").join(" ");
}

/** Turns anything thrown during the swap into one short sentence for the user. */
export function swapErrorMessage(error: unknown): string {
  if (error instanceof SwapSizingError || error instanceof SwapSafetyError) return error.message;
  if (isUserRejection(error) || isUserRejection((error as { cause?: unknown } | null)?.cause)) {
    return walletErrorMessage({ code: 4001 });
  }
  const text = errorText(error);
  if (/insufficient funds|out of gas|gas required exceeds|exceeds the balance of the account|intrinsic gas/i.test(text)) {
    return MSG_GAS;
  }
  if (/ONCHAIN_SIMULATION_FAILED|slippage|price impact|stop limit|simulation/i.test(text)) return MSG_LIQUIDITY;
  const message = (error as { message?: unknown } | null)?.message;
  return typeof message === "string" && message.length > 0 && message.length < 160 ? message : MSG_GENERIC;
}

/** A link from the swap result is only shown when it is a plain https URL. */
export function safeExplorerUrl(result: Pick<SwapResult, "explorerUrl" | "txHash">, explorerBase: string): string | null {
  const url = result.explorerUrl || (result.txHash ? `${explorerBase.replace(/\/+$/, "")}/tx/${result.txHash}` : "");
  return /^https:\/\//i.test(url) ? url : null;
}

// ---------------------------------------------------------------------------------------------------------------
// Wallet and SDK glue (lazy-loaded)
// ---------------------------------------------------------------------------------------------------------------

export interface PromptHooks {
  /** The wallet is about to ask the user to confirm something. */
  onPrompt?: () => void;
  /** The wallet has answered, so the transaction is on its way. */
  onSent?: () => void;
}

const PROMPTING_METHODS = new Set(["eth_sendTransaction", "eth_signTypedData_v4", "eth_signTypedData", "personal_sign"]);

/** Wraps the app's provider so the UI can tell "confirm in your wallet" from "swapping". Everything else passes through. */
export function observeProvider(provider: Eip1193Provider, hooks: PromptHooks): Eip1193Provider {
  return new Proxy(provider, {
    get(target, prop, receiver) {
      if (prop === "request") {
        return async (args: { method: string; params?: unknown[] }) => {
          const prompting = PROMPTING_METHODS.has(args.method);
          if (prompting) hooks.onPrompt?.();
          const result = await target.request(args);
          if (prompting) hooks.onSent?.();
          return result;
        };
      }
      const value = Reflect.get(target, prop, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

export interface ArcSwapper {
  estimate(amountIn: bigint): Promise<SwapEstimate>;
  swap(amountIn: bigint): Promise<SwapResult>;
}

/** Loads Swap Kit and the viem adapter on demand and binds them to the user's wallet. */
export async function createArcSwapper(provider: Eip1193Provider, hooks: PromptHooks = {}): Promise<ArcSwapper> {
  const [{ SwapKit }, { createViemAdapterFromProvider }] = await Promise.all([
    import("@circle-fin/swap-kit"),
    import("@circle-fin/adapter-viem-v2"),
  ]);
  // The app's provider is a plain EIP-1193 object; viem's type is stricter about event typings only.
  const adapter = await createViemAdapterFromProvider({
    provider: observeProvider(provider, hooks) as unknown as EIP1193Provider,
  });
  // No Circle API key is used in the browser, so requests are keyless.
  const kit = new SwapKit();
  const params = (amountIn: bigint): SwapParams => ({
    from: { adapter, chain: ARC_SWAP_CHAIN },
    tokenIn: "EURC",
    tokenOut: "USDC",
    amountIn: unitsToDecimal(amountIn),
    config: { slippageBps: SLIPPAGE_BPS },
  });
  return {
    estimate: (amountIn) => kit.estimate(params(amountIn)),
    swap: (amountIn) => kit.swap(params(amountIn)),
  };
}

/** The user's EURC balance on Arc, read straight from the chain's RPC. */
export async function readEurcBalance(rpc: string, address: string): Promise<bigint> {
  const { createPublicClient, http, parseAbi } = await import("viem");
  const client = createPublicClient({ transport: http(rpc) });
  return client.readContract({
    address: EURC_ADDRESS,
    abi: parseAbi(["function balanceOf(address owner) view returns (uint256)"]),
    functionName: "balanceOf",
    args: [address as `0x${string}`],
  });
}
