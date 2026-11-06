import type { Page } from "@playwright/test";
import { privateKeyToAccount } from "viem/accounts";

/**
 * A wallet injected into the page through EIP-6963, signing with a real key in the test process.
 * It records every request, and can start on another chain, not know a chain, or decline a request.
 */
export interface WalletOptions {
  key: `0x${string}`;
  startChainId?: string;
  knownChains?: string[];
  rejectMethods?: string[];
}

export async function injectWallet(page: Page, options: WalletOptions) {
  const account = privateKeyToAccount(options.key);
  const state = {
    chainId: options.startChainId ?? "0x1",
    known: new Set(options.knownChains ?? ["0x1", "0x4cef52", "0x752"]),
    reject: new Set(options.rejectMethods ?? []),
    calls: [] as { method: string; params?: unknown[] }[],
  };

  await page.exposeFunction("__walletRequest", async (method: string, params?: unknown[]) => {
    state.calls.push({ method, params });
    if (state.reject.delete(method)) return { error: { code: 4001, message: "User rejected the request." } };
    switch (method) {
      case "eth_requestAccounts":
      case "eth_accounts":
        return { result: [account.address.toLowerCase()] };
      case "eth_chainId":
        return { result: state.chainId };
      case "wallet_switchEthereumChain": {
        const { chainId } = (params as [{ chainId: string }])[0];
        if (!state.known.has(chainId)) return { error: { code: 4902, message: "Unrecognized chain ID" } };
        state.chainId = chainId;
        return { result: null };
      }
      case "wallet_addEthereumChain":
        state.known.add((params as [{ chainId: string }])[0].chainId);
        return { result: null };
      case "eth_signTypedData_v4": {
        const typed = JSON.parse((params as [string, string])[1]);
        const wanted = Number(typed.domain.chainId);
        // Real wallets refuse typed data for a chain other than the active one.
        if (wanted !== Number(state.chainId)) {
          return { error: { code: -32000, message: `Provided chainId ${wanted} must match the active chainId ${Number(state.chainId)}` } };
        }
        const { EIP712Domain: _domainType, ...types } = typed.types;
        const signature = await account.signTypedData({ domain: typed.domain, types, primaryType: typed.primaryType, message: typed.message });
        return { result: signature };
      }
      default:
        return { error: { code: 4200, message: `Unsupported ${method}` } };
    }
  });

  await page.addInitScript(() => {
    const provider = {
      isMetaMask: true,
      async request({ method, params }: { method: string; params?: unknown[] }) {
        // @ts-expect-error exposed by the test
        const reply = await window.__walletRequest(method, params);
        if (reply.error) throw Object.assign(new Error(reply.error.message), { code: reply.error.code });
        return reply.result;
      },
      on() {},
      removeListener() {},
    };
    const announce = () =>
      window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {
        detail: { info: { uuid: "test-wallet", rdns: "test.wallet", name: "Test Wallet", icon: "data:image/png;base64,iVBORw0KGgo=" }, provider },
      }));
    window.addEventListener("eip6963:requestProvider", announce);
    announce();
  });

  return { account, state };
}
