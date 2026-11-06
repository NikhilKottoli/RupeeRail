import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, type ChainInfo } from "./api";
import { describeStatus, formatInr, formatUnits } from "./format";
import { localPayments, rememberPayment } from "./localPayments";
import { paiseToRupeesString, parseUpiQr, QrError, rupeesToPaise } from "./upi";
import {
  connectWallet, discoverWallets, ensureChain, isUserRejection, metaMaskDeepLink, signTypedData, splitSignature,
  walletErrorMessage, type Eip1193Provider, type WalletOption,
} from "./wallet";

const R = "ab".repeat(32);
const S = "cd".repeat(32);

function fakeProvider(handlers: Record<string, (params?: unknown[]) => unknown>) {
  const calls: { method: string; params?: unknown[] }[] = [];
  const provider: Eip1193Provider = {
    request: async ({ method, params }) => {
      calls.push({ method, params });
      const handler = handlers[method];
      if (!handler) throw new Error(`unexpected ${method}`);
      return handler(params);
    },
  };
  return { provider, calls };
}

const ARC = { chainId: 5042002, name: "Arc Testnet", rpc: "https://rpc", explorer: "https://exp" } as ChainInfo;

describe("parseUpiQr", () => {
  it("reads a personal QR", () => {
    expect(parseUpiQr("upi://pay?pa=shop@ybl&pn=Chai%20Point&cu=INR")).toMatchObject({ upiId: "shop@ybl", name: "Chai Point", amountPaise: null });
  });
  it("reads amounts in paise without float errors", () => {
    expect(parseUpiQr("upi://pay?pa=ab@ybl&am=19.99").amountPaise).toBe(1999);
    expect(parseUpiQr("upi://pay?pa=ab@ybl&am=10.5").amountPaise).toBe(1050);
    expect(parseUpiQr("UPI://PAY?pa=ab@ybl&AM=10").amountPaise).toBe(1000);
  });
  it("uses the first occurrence, as the backend does", () => {
    expect(parseUpiQr("upi://pay?pa=first@ybl&pa=evil@ybl").upiId).toBe("first@ybl");
  });
  it("treats an empty amount as none", () => {
    expect(parseUpiQr("upi://pay?pa=ab@ybl&am=").amountPaise).toBeNull();
  });
  it.each([
    ["", "empty"], ["https://example.com", "isn't a UPI"], ["upi://pay?pn=x", "valid UPI ID"],
    ["upi://pay?pa=bad", "valid UPI ID"], ["upi://pay?pa=ab@ybl&cu=USD", "INR"], ["upi://pay?pa=ab@ybl&am=1.234", "invalid amount"],
    ["000201010212", "isn't supported"], [`upi://pay?pa=ab@ybl&tn=${"x".repeat(600)}`, "too long"],
  ])("rejects %s", (text, message) => {
    expect(() => parseUpiQr(text)).toThrow(QrError);
    expect(() => parseUpiQr(text)).toThrow(message);
  });
  it("keeps the raw text for the backend's commitment", () => {
    const raw = "upi://pay?pa=ab@ybl&pn=A%20B";
    expect(parseUpiQr(raw).qrText).toBe(raw);
  });
});

describe("rupees", () => {
  it.each([["1", 100], ["1000", 100000], ["12.5", 1250], ["0.29", 29], ["1.10", 110], [" 5 ", 500]])("%s", (input, paise) => {
    expect(rupeesToPaise(input)).toBe(paise);
  });
  it.each(["", "abc", "1.234", "-1", "1e3", "1,000"])("rejects %s", (input) => {
    expect(rupeesToPaise(input)).toBeNull();
  });
  it("round-trips to what the backend parses", () => {
    expect(paiseToRupeesString(100)).toBe("1");
    expect(paiseToRupeesString(1205)).toBe("12.05");
  });
});

describe("format", () => {
  it("formats rupees and token units", () => {
    expect(formatInr("100")).toBe("₹1");
    expect(formatInr(123456)).toBe("₹1,234.56");
    expect(formatInr(2550)).toBe("₹25.50");
    expect(formatUnits("10106", 6)).toBe("0.010106");
    expect(formatUnits("1000000", 6)).toBe("1");
  });
  it("doesn't claim the merchant was paid when the operator only reported it", () => {
    const text = describeStatus({ status: "COMPLETED", payout: "DISPATCHED", refundReasonName: null });
    expect(text.title).toBe("Payment processed");
    expect(text.detail).toContain("dispute");
    expect(text.active).toBe(false);
  });
  it("keeps polling while a payment can still change", () => {
    for (const status of ["SUBMITTED", "LOCKED", "DISPUTED"]) {
      expect(describeStatus({ status, payout: "NONE", refundReasonName: null }).active).toBe(true);
    }
    for (const status of ["REFUNDED", "REVERSED", "RELEASED"]) {
      expect(describeStatus({ status, payout: "NONE", refundReasonName: null }).active).toBe(false);
    }
  });
});

describe("splitSignature", () => {
  it.each([["1b", 27], ["1c", 28], ["00", 27], ["01", 28]])("normalises v=%s", (v, expected) => {
    expect(splitSignature(`0x${R}${S}${v}`)).toEqual({ r: `0x${R}`, s: `0x${S}`, v: expected });
  });
  it("rejects malformed signatures", () => {
    expect(() => splitSignature("0x1234")).toThrow();
  });
});

describe("wallet", () => {
  afterEach(() => {
    delete window.ethereum;
    vi.useRealTimers();
  });

  it("does nothing when already on the chain", async () => {
    const { provider, calls } = fakeProvider({ eth_chainId: () => "0x4CEF52" });
    await ensureChain(provider, "arc", ARC);
    expect(calls.map((c) => c.method)).toEqual(["eth_chainId"]);
  });
  it("switches chain", async () => {
    const { provider, calls } = fakeProvider({ eth_chainId: () => "0x1", wallet_switchEthereumChain: () => null });
    await ensureChain(provider, "arc", ARC);
    expect(calls[1]).toEqual({ method: "wallet_switchEthereumChain", params: [{ chainId: "0x4cef52" }] });
  });
  it("adds an unknown chain with its gas coin, then switches", async () => {
    let switches = 0;
    const { provider, calls } = fakeProvider({
      eth_chainId: () => "0x1",
      wallet_switchEthereumChain: () => {
        if (++switches === 1) throw Object.assign(new Error("Unrecognized chain"), { code: 4902 });
        return null;
      },
      wallet_addEthereumChain: () => null,
    });
    await ensureChain(provider, "arc", ARC);
    expect(calls.map((c) => c.method)).toEqual(["eth_chainId", "wallet_switchEthereumChain", "wallet_addEthereumChain", "wallet_switchEthereumChain"]);
    expect(calls[2].params?.[0]).toMatchObject({ chainName: "Arc Testnet", nativeCurrency: { symbol: "USDC", decimals: 18 }, rpcUrls: ["https://rpc"] });
  });
  it("doesn't try adding a chain after the user declines", async () => {
    const { provider, calls } = fakeProvider({
      eth_chainId: () => "0x1",
      wallet_switchEthereumChain: () => {
        throw Object.assign(new Error("User rejected"), { code: 4001 });
      },
    });
    await expect(ensureChain(provider, "arc", ARC)).rejects.toMatchObject({ code: 4001 });
    expect(calls.map((c) => c.method)).not.toContain("wallet_addEthereumChain");
  });
  it("signs the backend's payload unchanged, as a JSON string", async () => {
    const typed = { domain: { name: "x" }, types: {}, primaryType: "P", message: { a: "1" } };
    const { provider, calls } = fakeProvider({ eth_signTypedData_v4: () => `0x${R}${S}1b` });
    await signTypedData(provider, "0xabc", typed);
    expect(calls[0].params).toEqual(["0xabc", JSON.stringify(typed)]);
  });
  it("rejects a malformed signature or account from a wallet", async () => {
    await expect(signTypedData(fakeProvider({ eth_signTypedData_v4: () => "0x12" }).provider, "0x1", {} as never)).rejects.toThrow("unexpected signature");
    await expect(connectWallet(fakeProvider({ eth_requestAccounts: () => [] }).provider)).rejects.toThrow("didn't share");
    await expect(connectWallet(fakeProvider({ eth_requestAccounts: () => ["javascript:alert(1)"] }).provider)).rejects.toThrow("didn't share");
  });
  it("explains wallet errors", () => {
    expect(isUserRejection({ code: 4001 })).toBe(true);
    expect(walletErrorMessage({ code: 4001 })).toContain("cancelled");
    expect(walletErrorMessage({ code: -32002 })).toContain("already has a request");
    expect(walletErrorMessage({ message: "x".repeat(500) })).toContain("couldn't complete");
  });
  it("discovers EIP-6963 wallets, ignoring unsafe icons", () => {
    const seen: WalletOption[][] = [];
    const stop = discoverWallets((wallets) => seen.push(wallets));
    const provider = fakeProvider({}).provider;
    window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail: { info: { uuid: "1", rdns: "io.metamask", name: "MetaMask", icon: "data:image/png;base64,AA" }, provider } }));
    window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail: { info: { uuid: "2", rdns: "x.evil", name: "Evil", icon: "https://evil.example/a.png" }, provider } }));
    window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail: { info: { name: "No id" } } }));
    stop();
    const last = seen[seen.length - 1];
    expect(last.map((w) => w.name)).toEqual(["MetaMask", "Evil"]);
    expect(last[0].icon).toBeDefined();
    expect(last[1].icon).toBeUndefined();
  });
  it("falls back to window.ethereum only when nothing announces", () => {
    vi.useFakeTimers();
    window.ethereum = { ...fakeProvider({}).provider, isMetaMask: true };
    const seen: WalletOption[][] = [];
    const stop = discoverWallets((wallets) => seen.push(wallets));
    vi.advanceTimersByTime(400);
    stop();
    expect(seen).toHaveLength(1);
    expect(seen[0][0]).toMatchObject({ id: "injected", name: "MetaMask" });
  });
  it("reports an empty list when there's no wallet at all", () => {
    vi.useFakeTimers();
    const seen: WalletOption[][] = [];
    const stop = discoverWallets((wallets) => seen.push(wallets));
    vi.advanceTimersByTime(400);
    stop();
    expect(seen).toEqual([[]]);
  });
  it("builds the MetaMask deep link without the scheme", () => {
    expect(metaMaskDeepLink(new URL("https://app.example/pay?x=1"))).toBe("https://metamask.app.link/dapp/app.example/pay?x=1");
  });
});

describe("api", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("maps backend errors to friendly messages", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "DAILY_LIMIT", message: "raw" }), { status: 429 })));
    const error = await api.config().catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("DAILY_LIMIT");
    expect(error.message).toContain("today's payment limit");
  });
  it("keeps the backend's message for codes it doesn't know", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "NEW", message: "specific" }), { status: 400 })));
    await expect(api.config()).rejects.toThrow("specific");
  });
  it("explains network failures and non-JSON errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("failed"); }));
    await expect(api.config()).rejects.toThrow("connection");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>", { status: 502 })));
    await expect(api.config()).rejects.toThrow("went wrong");
  });
  it("sends the view token as a header, never in the URL", async () => {
    const fetchMock = vi.fn(async () => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await api.offramp("0xabc", "secret-token");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).not.toContain("secret-token");
    expect(new Headers(init.headers).get("x-view-token")).toBe("secret-token");
  });
});

describe("localPayments", () => {
  it("stores per wallet, case-insensitively", () => {
    const record = { viewToken: "t", upiId: "a@ybl", name: null, createdAt: 1 };
    rememberPayment("0xAAA", "0xID", record);
    expect(localPayments("0xaaa")["0xid"]).toEqual(record);
    expect(localPayments("0xbbb")).toEqual({});
  });
  it("survives corrupt storage", () => {
    localStorage.setItem("rupeerail.payments.0xccc", "{not json");
    expect(localPayments("0xccc")).toEqual({});
  });
});
