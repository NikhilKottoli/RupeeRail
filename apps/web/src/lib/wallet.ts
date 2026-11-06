/**
 * Browser wallets, through plain EIP-1193 and EIP-6963.
 *
 * The backend builds every typed-data payload, so the app only needs to connect, switch chain and sign.
 * The rest of the app depends only on Eip1193Provider, so another connector such as WalletConnect can
 * supply a provider without changing it.
 */

import type { ChainInfo, ChainKey, TypedData } from "./api";

export interface Eip1193Provider {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  request(args: { method: string; params?: unknown[] }): Promise<any>;
  on?(event: string, listener: (...args: never[]) => void): void;
  removeListener?(event: string, listener: (...args: never[]) => void): void;
}

export interface WalletOption {
  id: string;
  name: string;
  icon?: string;
  provider: Eip1193Provider;
}

declare global {
  interface Window {
    ethereum?: Eip1193Provider & { isMetaMask?: boolean };
  }
}

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
const SIGNATURE_RE = /^0x[0-9a-fA-F]{130}$/;

/** Gas coins, which /config doesn't include. Wallets need them to add a chain they don't know. */
const NATIVE_CURRENCY: Record<ChainKey, { name: string; symbol: string; decimals: number }> = {
  arc: { name: "USDC", symbol: "USDC", decimals: 18 },
  whitechain: { name: "Whitechain", symbol: "WBT", decimals: 18 },
};

/** Calls `onChange` with every wallet found. The legacy window.ethereum is used only if none announce themselves. */
export function discoverWallets(onChange: (wallets: WalletOption[]) => void, legacyDelayMs = 300): () => void {
  const found = new Map<string, WalletOption>();
  const onAnnounce = (event: Event) => {
    const { info, provider } = (event as CustomEvent).detail ?? {};
    if (!info?.uuid || !provider) return;
    found.set(info.rdns ?? info.uuid, {
      id: info.rdns ?? info.uuid,
      name: String(info.name ?? "Browser wallet"),
      icon: isSafeIcon(info.icon) ? info.icon : undefined,
      provider,
    });
    onChange([...found.values()]);
  };
  window.addEventListener("eip6963:announceProvider", onAnnounce);
  window.dispatchEvent(new Event("eip6963:requestProvider"));
  const timer = window.setTimeout(() => {
    if (found.size === 0 && window.ethereum) {
      const name = window.ethereum.isMetaMask ? "MetaMask" : "Browser wallet";
      onChange([{ id: "injected", name, provider: window.ethereum }]);
    } else if (found.size === 0) {
      onChange([]);
    }
  }, legacyDelayMs);
  return () => {
    window.removeEventListener("eip6963:announceProvider", onAnnounce);
    window.clearTimeout(timer);
  };
}

/** Wallet icons come from whatever extension announced them, so only image data URIs are rendered. */
function isSafeIcon(icon: unknown): icon is string {
  return typeof icon === "string" && /^data:image\/(png|jpeg|webp|gif|svg\+xml)[;,]/.test(icon);
}

function firstAddress(accounts: unknown): string {
  const address = Array.isArray(accounts) ? accounts[0] : undefined;
  if (typeof address !== "string" || !ADDRESS_RE.test(address)) throw new Error("The wallet didn't share an account.");
  return address;
}

export async function connectWallet(provider: Eip1193Provider): Promise<string> {
  return firstAddress(await provider.request({ method: "eth_requestAccounts" }));
}

/** The account the wallet has already approved for this site, without prompting. */
export async function existingAccount(provider: Eip1193Provider): Promise<string | null> {
  try {
    return firstAddress(await provider.request({ method: "eth_accounts" }));
  } catch {
    return null;
  }
}

/** Wallets refuse to sign typed data for a chain other than the active one. */
export async function ensureChain(provider: Eip1193Provider, key: ChainKey, chain: ChainInfo): Promise<void> {
  const chainId = `0x${chain.chainId.toString(16)}`;
  const current = await provider.request({ method: "eth_chainId" }).catch(() => null);
  if (typeof current === "string" && current.toLowerCase() === chainId) return;
  try {
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId }] });
  } catch (e) {
    if (isUserRejection(e)) throw e;
    // 4902 is the standard "unknown chain" code, but mobile wallets report it in other ways too.
    await provider.request({
      method: "wallet_addEthereumChain",
      params: [{
        chainId,
        chainName: chain.name,
        nativeCurrency: NATIVE_CURRENCY[key] ?? { name: "Native", symbol: "ETH", decimals: 18 },
        rpcUrls: [chain.rpc],
        blockExplorerUrls: [chain.explorer],
      }],
    });
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId }] });
  }
}

export async function signTypedData(provider: Eip1193Provider, address: string, typed: TypedData): Promise<string> {
  const signature = await provider.request({ method: "eth_signTypedData_v4", params: [address, JSON.stringify(typed)] });
  if (typeof signature !== "string" || !SIGNATURE_RE.test(signature)) throw new Error("The wallet returned an unexpected signature.");
  return signature;
}

/** Splits a 65-byte signature for the token permit. Some wallets return v as 0 or 1 instead of 27 or 28. */
export function splitSignature(signature: string): { r: string; s: string; v: number } {
  if (!SIGNATURE_RE.test(signature)) throw new Error("The wallet returned an unexpected signature.");
  const v = parseInt(signature.slice(130, 132), 16);
  return { r: `0x${signature.slice(2, 66)}`, s: `0x${signature.slice(66, 130)}`, v: v < 27 ? v + 27 : v };
}

export function isUserRejection(error: unknown): boolean {
  const e = error as { code?: unknown; message?: unknown } | null;
  return e?.code === 4001 || e?.code === "ACTION_REJECTED" || /user (rejected|denied|cancel)/i.test(String(e?.message ?? ""));
}

export function walletErrorMessage(error: unknown): string {
  if (isUserRejection(error)) return "You cancelled this in your wallet.";
  const e = error as { code?: unknown; message?: unknown } | null;
  if (e?.code === -32002) return "Your wallet already has a request open. Check the wallet.";
  const message = typeof e?.message === "string" ? e.message : "";
  return message && message.length < 160 ? message : "Your wallet couldn't complete this. Please try again.";
}

/** Link that opens this app inside MetaMask Mobile's browser, where a wallet is available. */
export function metaMaskDeepLink(url: URL): string {
  return `https://metamask.app.link/dapp/${url.host}${url.pathname}${url.search}`;
}

export function isMobileDevice(userAgent = navigator.userAgent): boolean {
  return /android|iphone|ipad|ipod|mobile/i.test(userAgent);
}
