import { describe, expect, it, vi } from "vitest";
import {
  assertEstimateSafe, bumpAmount, GAS_RESERVE, gasFeeText, gasFeeUnits, MAX_SIZING_TRIES, requiredOut, MSG_GAS, MSG_LIQUIDITY, MSG_NOT_ENOUGH_EURC,
  observeProvider, safeExplorerUrl, shouldOfferSwap, sizeSwap, SwapSafetyError, SwapSizingError, swapErrorMessage,
  swapTarget, unitsFromDecimal, unitsToDecimal,
} from "./swap";
import type { Eip1193Provider } from "./wallet";

const USDC = (n: number) => BigInt(Math.round(n * 1e6));

/** A fake pool: the stop limit is `rate` x amountIn, rounded down. */
function pool(rate: number, calls: bigint[] = []) {
  return async (amountIn: bigint) => {
    calls.push(amountIn);
    return { minOut: (amountIn * BigInt(Math.round(rate * 1e6))) / 1_000_000n };
  };
}

describe("amount maths", () => {
  it("reads decimals into base units without float errors", () => {
    expect(unitsFromDecimal("1.5")).toBe(1_500_000n);
    expect(unitsFromDecimal("0.000001")).toBe(1n);
    expect(unitsFromDecimal("12")).toBe(12_000_000n);
    expect(unitsFromDecimal("1.2345679")).toBe(1_234_567n);
    expect(unitsFromDecimal("1.2345679", 6, "ceil")).toBe(1_234_568n);
    expect(unitsFromDecimal("1.2345670", 6, "ceil")).toBe(1_234_567n);
    expect(() => unitsFromDecimal("1e6")).toThrow();
    expect(() => unitsFromDecimal("-1")).toThrow();
  });
  it("writes base units as the decimal string the kit expects", () => {
    expect(unitsToDecimal(1_500_000n)).toBe("1.5");
    expect(unitsToDecimal(2_000_000n)).toBe("2");
    expect(unitsToDecimal(1n)).toBe("0.000001");
    expect(unitsToDecimal(0n)).toBe("0");
  });
  it("targets the shortfall plus 3% plus 0.05 USDC", () => {
    expect(swapTarget(1_000_000n)).toBe(1_080_000n);
    expect(swapTarget(1_500_000n)).toBe(1_595_000n);
    // rounds the 3% up, never down
    expect(swapTarget(1n)).toBe(50_002n);
  });
  it("bumps a try by 2%, rounding up", () => {
    expect(bumpAmount(1_000_000n)).toBe(1_020_000n);
    expect(bumpAmount(1n)).toBe(2n);
  });
});

describe("sizeSwap", () => {
  it("sizes from the minimum output rate, then confirms with a second estimate", async () => {
    const calls: bigint[] = [];
    const shortfall = USDC(1.5);
    const out = await sizeSwap({ shortfall, eurcBalance: USDC(100), estimate: pool(1.08, calls) });
    // rate 1.08, target 1.595: amountIn = ceil(1.595 / 1.08) = 1.476852 EURC
    expect(out.amountIn).toBe(1_476_852n);
    expect(out.quote.minOut).toBeGreaterThanOrEqual(shortfall);
    expect(out.quote.minOut).toBeGreaterThanOrEqual(1_595_000n - 2n);
    expect(calls).toEqual([1_595_000n, 1_476_852n]);
    expect(out.estimates).toBe(2);
  });

  it("includes the 3% buffer and the 0.05 USDC allowance in what it asks for", async () => {
    const out = await sizeSwap({ shortfall: USDC(10), eurcBalance: USDC(1000), estimate: pool(1) });
    expect(out.amountIn).toBe(10_350_000n); // 10 x 1.03 + 0.05
    expect(out.quote.minOut).toBe(10_350_000n);
  });

  it("reuses the probe when the guess was already exact", async () => {
    const calls: bigint[] = [];
    const out = await sizeSwap({ shortfall: USDC(1), eurcBalance: USDC(100), estimate: pool(1, calls) });
    expect(out.amountIn).toBe(1_080_000n);
    expect(calls).toEqual([1_080_000n]);
    expect(out.estimates).toBe(1);
  });

  it("raises the amount by 2% and retries when the minimum output falls short", async () => {
    const calls: bigint[] = [];
    // The pool gets worse for bigger orders: the probe looks good, the real order doesn't.
    const estimate = async (amountIn: bigint) => {
      calls.push(amountIn);
      const rate = calls.length === 1 ? 1.1 : calls.length === 2 ? 1.0 : 1.06;
      return { minOut: BigInt(Math.floor(Number(amountIn) * rate)) };
    };
    const out = await sizeSwap({ shortfall: USDC(1), eurcBalance: USDC(100), estimate });
    expect(calls).toHaveLength(3);
    expect(calls[2]).toBe(bumpAmount(calls[1]));
    expect(out.amountIn).toBe(calls[2]);
    expect(out.quote.minOut).toBeGreaterThanOrEqual(USDC(1));
  });

  it("gives up after 3 tries", async () => {
    const calls: bigint[] = [];
    // The probe looks generous (rate 1.2), but every real order comes back short of the 1 USDC shortfall.
    const estimate = async (amountIn: bigint) => {
      calls.push(amountIn);
      return { minOut: calls.length === 1 ? (amountIn * 12n) / 10n : USDC(0.9) };
    };
    await expect(sizeSwap({ shortfall: USDC(1), eurcBalance: USDC(100), estimate })).rejects.toMatchObject({ code: "NO_QUOTE" });
    expect(calls).toHaveLength(1 + MAX_SIZING_TRIES);
  });

  it("never sells more than the EURC balance, and says so when the balance can't cover the shortfall", async () => {
    // Needs about 1.48 EURC, has 1.
    await expect(sizeSwap({ shortfall: USDC(1.5), eurcBalance: USDC(1), estimate: pool(1.08) }))
      .rejects.toThrow(MSG_NOT_ENOUGH_EURC);
    await expect(sizeSwap({ shortfall: USDC(1.5), eurcBalance: 0n, estimate: pool(1.08) }))
      .rejects.toMatchObject({ code: "NOT_ENOUGH_EURC" });
  });

  it("caps at the balance when the buffer doesn't fit but shortfall + gas still does", async () => {
    const calls: bigint[] = [];
    // Target 1.595 USDC needs about 1.477 EURC. The wallet has 1.4 EURC, which returns 1.512 USDC >= 1.5 + 0.01 gas.
    const estimate = async (amountIn: bigint) => ({ ...(await pool(1.08, calls)(amountIn)), gasFee: USDC(0.01) });
    const out = await sizeSwap({ shortfall: USDC(1.5), eurcBalance: USDC(1.4), estimate });
    expect(out.amountIn).toBe(USDC(1.4));
    expect(out.quote.minOut).toBeGreaterThanOrEqual(USDC(1.51));
    expect(Math.max(...calls.map(Number))).toBeLessThanOrEqual(Number(USDC(1.4)));
  });

  it("refuses at the balance cap when the whole balance covers the shortfall but not the gas", async () => {
    // 1.4 EURC returns 1.512 USDC: enough for the 1.5 shortfall, not for 1.5 + 0.02 gas.
    const estimate = async (amountIn: bigint) => ({ ...(await pool(1.08)(amountIn)), gasFee: USDC(0.02) });
    await expect(sizeSwap({ shortfall: USDC(1.5), eurcBalance: USDC(1.4), estimate }))
      .rejects.toMatchObject({ code: "NOT_ENOUGH_EURC" });
  });

  it("rejects an empty quote and a payment that needs no swap", async () => {
    await expect(sizeSwap({ shortfall: USDC(1), eurcBalance: USDC(5), estimate: async () => ({ minOut: 0n }) }))
      .rejects.toMatchObject({ code: "NO_QUOTE" });
    await expect(sizeSwap({ shortfall: 0n, eurcBalance: USDC(5), estimate: pool(1) }))
      .rejects.toBeInstanceOf(SwapSizingError);
  });
});

describe("gas is paid from the same USDC", () => {
  it("does not accept a minimum output equal to the shortfall when the swap costs gas", async () => {
    const calls: bigint[] = [];
    const shortfall = USDC(1);
    const gasFee = USDC(0.02);
    // The probe looks generous (rate 1.1); the sized order comes back at exactly the shortfall, which the old rule
    // accepted; the bumped one covers shortfall + gas.
    const estimate = async (amountIn: bigint) => {
      calls.push(amountIn);
      const minOut = calls.length === 1 ? (amountIn * 11n) / 10n : calls.length === 2 ? shortfall : shortfall + gasFee;
      return { minOut, gasFee };
    };
    const out = await sizeSwap({ shortfall, eurcBalance: USDC(100), estimate });
    expect(calls).toHaveLength(3);
    expect(calls[2]).toBe(bumpAmount(calls[1]));
    expect(out.quote.minOut).toBe(shortfall + gasFee);
  });

  it("uses the 0.05 USDC reserve when an estimate reports no gas", async () => {
    // 1.4 EURC returns 1.512 USDC, which is short of 1.5 + 0.05.
    await expect(sizeSwap({ shortfall: USDC(1.5), eurcBalance: USDC(1.4), estimate: pool(1.08) }))
      .rejects.toMatchObject({ code: "NOT_ENOUGH_EURC" });
    const estimate = async (amountIn: bigint) => ({ ...(await pool(1.08)(amountIn)), gasFee: null });
    await expect(sizeSwap({ shortfall: USDC(1.5), eurcBalance: USDC(1.4), estimate }))
      .rejects.toMatchObject({ code: "NOT_ENOUGH_EURC" });
    expect(requiredOut(USDC(1.5), null)).toBe(USDC(1.5) + GAS_RESERVE);
    expect(requiredOut(USDC(1.5), undefined)).toBe(USDC(1.55));
  });

  it("aims for shortfall + gas when gas is bigger than the 3% + 0.05 buffer", async () => {
    const calls: bigint[] = [];
    // Shortfall 1 USDC: the buffer target is 1.08, but gas is 0.2, so it must aim for 1.2.
    const estimate = async (amountIn: bigint) => ({ ...(await pool(1, calls)(amountIn)), gasFee: USDC(0.2) });
    const out = await sizeSwap({ shortfall: USDC(1), eurcBalance: USDC(100), estimate });
    expect(calls).toEqual([USDC(1.08), USDC(1.2)]);
    expect(out.amountIn).toBe(USDC(1.2));
    expect(out.quote.minOut).toBeGreaterThanOrEqual(USDC(1.2));
  });

  it("reads the gas fee only when it is reported in USDC, rounded up", () => {
    expect(gasFeeUnits({ fees: [{ type: "provider", token: "EURC", amount: "0.0002" }, { type: "gas", token: "USDC", amount: "0.017489471475" }] } as never)).toBe(17_490n);
    expect(gasFeeUnits({ fees: [{ type: "gas", token: "usdc", amount: "0.003" }] } as never)).toBe(3_000n);
    // A gas fee in another token can't be netted against the USDC balance, so the caller uses the reserve.
    expect(gasFeeUnits({ fees: [{ type: "gas", token: "EURC", amount: "0.003" }] } as never)).toBeNull();
    expect(gasFeeUnits({ fees: [{ type: "gas", token: "USDC", amount: null }] } as never)).toBeNull();
    expect(gasFeeUnits({ fees: [{ type: "gas", token: "USDC", amount: "n/a" }] } as never)).toBeNull();
    expect(gasFeeUnits({ fees: [{ type: "provider", token: "EURC", amount: "0.0002" }] } as never)).toBeNull();
    expect(gasFeeUnits({} as never)).toBeNull();
  });
});

const GOOD = {
  tokenIn: "EURC", tokenOut: "USDC", chainIn: "Arc_Testnet", chainOut: "Arc_Testnet",
  fromAddress: "0xAbCdEf0000000000000000000000000000000001", toAddress: "0xabcdef0000000000000000000000000000000001",
} as never;
const ADDRESS = "0xABCDEF0000000000000000000000000000000001";

describe("assertEstimateSafe", () => {
  it("accepts the right swap, comparing addresses without case", () => {
    expect(() => assertEstimateSafe(GOOD, ADDRESS)).not.toThrow();
  });
  it("accepts an estimate with no address fields", () => {
    expect(() => assertEstimateSafe({ ...(GOOD as object), fromAddress: "", toAddress: "" } as never, ADDRESS)).not.toThrow();
  });
  it("refuses a different recipient, wallet, token or network", () => {
    const base = GOOD as unknown as Record<string, string>;
    const bad = (patch: Record<string, string>) => () => assertEstimateSafe({ ...base, ...patch } as never, ADDRESS);
    expect(bad({ toAddress: "0x0000000000000000000000000000000000000002" })).toThrow(SwapSafetyError);
    expect(bad({ fromAddress: "0x0000000000000000000000000000000000000002" })).toThrow(SwapSafetyError);
    expect(bad({ tokenOut: "EURC" })).toThrow(SwapSafetyError);
    expect(bad({ tokenIn: "USDC" })).toThrow(SwapSafetyError);
    expect(bad({ chainIn: "Ethereum" })).toThrow(SwapSafetyError);
    expect(bad({ chainOut: "Base" })).toThrow(SwapSafetyError);
  });
});

describe("messages and links", () => {
  it("finds the gas fee in the estimate", () => {
    expect(gasFeeText({ fees: [{ type: "provider", token: "EURC", amount: "0.0002" }, { type: "gas", token: "USDC", amount: "0.003" }] })).toBe("0.003 USDC");
    // The real service reports gas with 12 decimals.
    expect(gasFeeText({ fees: [{ type: "gas", token: "USDC", amount: "0.017489471475" }] })).toBe("0.01749 USDC");
    expect(gasFeeText({ fees: [{ type: "gas", token: "USDC", amount: null }] })).toBeNull();
    expect(gasFeeText({})).toBeNull();
  });
  it("words each kind of failure for the user", () => {
    expect(swapErrorMessage({ code: 4001, message: "User rejected the request." })).toBe("You cancelled this in your wallet.");
    expect(swapErrorMessage({ message: "x", cause: { code: 4001 } })).toBe("You cancelled this in your wallet.");
    expect(swapErrorMessage(new Error("ONCHAIN_SIMULATION_FAILED: execution reverted"))).toBe(MSG_LIQUIDITY);
    expect(swapErrorMessage(new Error("Slippage exceeded"))).toBe(MSG_LIQUIDITY);
    expect(swapErrorMessage(new Error("insufficient funds for gas * price + value"))).toBe(MSG_GAS);
    expect(swapErrorMessage({ message: "x", cause: { message: "The total cost exceeds the balance of the account." } })).toBe(MSG_GAS);
    expect(swapErrorMessage(new SwapSizingError("NOT_ENOUGH_EURC"))).toBe(MSG_NOT_ENOUGH_EURC);
    expect(swapErrorMessage(new Error("Quote service is down"))).toBe("Quote service is down");
    expect(swapErrorMessage(new Error("x".repeat(400)))).toBe("The swap couldn't be completed. Please try again.");
    expect(swapErrorMessage(null)).toBe("The swap couldn't be completed. Please try again.");
  });
  it("only links to https explorers", () => {
    expect(safeExplorerUrl({ explorerUrl: "https://explorer.testnet.arc.io/tx/0x1", txHash: "0x1" }, "https://e")).toBe("https://explorer.testnet.arc.io/tx/0x1");
    expect(safeExplorerUrl({ explorerUrl: undefined, txHash: "0x1" }, "https://e.io/")).toBe("https://e.io/tx/0x1");
    expect(safeExplorerUrl({ explorerUrl: "javascript:alert(1)", txHash: "0x1" }, "https://e")).toBeNull();
    expect(safeExplorerUrl({ explorerUrl: undefined, txHash: "" }, "https://e")).toBeNull();
  });
  it("offers the swap only on Arc when the balance is too low", () => {
    expect(shouldOfferSwap({ chainId: 5042002 }, true)).toBe(true);
    expect(shouldOfferSwap({ chainId: 5042002 }, false)).toBe(false);
    expect(shouldOfferSwap({ chainId: 1874 }, true)).toBe(false);
  });
});

describe("observeProvider", () => {
  it("reports wallet prompts and passes every call through", async () => {
    const events: string[] = [];
    const inner = {
      request: vi.fn(async ({ method }: { method: string }) => (method === "eth_chainId" ? "0x1" : "0xhash")),
      on: vi.fn(),
    };
    const wrapped = observeProvider(inner as unknown as Eip1193Provider, {
      onPrompt: () => events.push("prompt"),
      onSent: () => events.push("sent"),
    });
    expect(await wrapped.request({ method: "eth_chainId" })).toBe("0x1");
    expect(events).toEqual([]);
    expect(await wrapped.request({ method: "eth_sendTransaction", params: [{}] })).toBe("0xhash");
    expect(events).toEqual(["prompt", "sent"]);
    wrapped.on?.("accountsChanged", () => undefined);
    expect(inner.on).toHaveBeenCalled();
  });
  it("doesn't report a prompt as sent when the user rejects it", async () => {
    const events: string[] = [];
    const inner = { request: vi.fn(async () => { throw Object.assign(new Error("no"), { code: 4001 }); }) };
    const wrapped = observeProvider(inner as unknown as Eip1193Provider, { onPrompt: () => events.push("prompt"), onSent: () => events.push("sent") });
    await expect(wrapped.request({ method: "eth_sendTransaction" })).rejects.toMatchObject({ code: 4001 });
    expect(events).toEqual(["prompt"]);
  });
});
