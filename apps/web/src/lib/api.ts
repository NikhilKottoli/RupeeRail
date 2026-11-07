/** Client for the RupeeRail API. Every integer in a response is a decimal string. */

const BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

export type ChainKey = string;

export interface TypedData {
  domain: Record<string, unknown>;
  types: Record<string, { name: string; type: string }[]>;
  primaryType: string;
  message: Record<string, unknown>;
}

export interface ChainInfo {
  chainId: number;
  name: string;
  rpc: string;
  explorer: string;
  vault: string;
  token: { symbol: string; address: string; decimals: number };
  vaultDomain: Record<string, unknown>;
  types: TypedData["types"];
}

export interface AppConfig {
  chains: Record<ChainKey, ChainInfo>;
  limits: { minInrPaise: string; maxInrPaise: string; dailyUserInrPaise: string };
  spreadBps: number;
  quoteTtlSec: number;
  /** Absent on a backend older than the Razorpay rail. */
  onramp?: { rail: string; testMode: boolean };
}

export interface Account {
  balance: string;
  balanceFormatted: string;
  symbol: string;
}

export interface Quote {
  id: string;
  intent: Record<string, string>;
  payee: { upiId: string; name: string | null; amountInQr: string | null };
  display: {
    amount: string;
    symbol: string;
    rate: string;
    marketRate: string;
    spreadBps: number;
    networkFee: string;
    expiresAt: string;
  };
  signing: { permit: TypedData; intent: TypedData };
  viewToken: string;
}

export interface OfframpView {
  id: string;
  chain: ChainKey;
  user: string | null;
  status: string;
  payout: string;
  payee: { upiId: string; name: string | null } | null;
  amount: string | null;
  inrPaise: string | null;
  refundReasonName: string | null;
  timers: { releasableAt: string | null; reclaimableAt: string | null; disputeDeadline: string | null };
  canDispute: boolean;
  txs: { lock: string | null; complete: string | null; dispute: string | null; final: string | null };
  explorer: string | null;
}

export type OnrampStatusName = "AWAITING_FIAT" | "PAYING" | "PAID" | "EXPIRED" | "FAILED";

/** An INR -> token purchase. `amount` is already in token units; `checkout` is null on the manual rail. */
export interface OnrampOrder {
  orderId: string;
  chain: ChainKey;
  wallet: string;
  inrPaise: string;
  amount: string;
  amountBaseUnits: string;
  symbol: string | null;
  rate: string;
  payTo: { upiId: string; note: string } | null;
  checkout: { provider: "razorpay"; url: string; linkId: string; expiresAt: string; paymentId: string | null } | null;
  status: OnrampStatusName | string;
  paidTx: string | null;
  explorer: string | null;
  lastError: string | null;
}

const FRIENDLY: Record<string, string> = {
  INSUFFICIENT_BALANCE: "Your wallet doesn't have enough for this payment.",
  QUOTE_EXPIRED: "That rate expired. Get a new one to continue.",
  INTENT_EXPIRED: "That rate expired. Get a new one to continue.",
  QUOTE_USED: "This quote has already been used. Start a new payment.",
  DAILY_LIMIT: "You've reached today's payment limit.",
  FX_STALE: "Exchange rates are updating. Try again in a moment.",
  RATE_LIMITED: "Too many requests. Wait a minute and try again.",
  RPC_UNAVAILABLE: "The network isn't responding. Try again in a moment.",
  NOT_DISPUTABLE: "This payment can't be disputed any more.",
  DISPUTE_IN_PROGRESS: "A dispute for this payment is already being sent.",
  BAD_INTENT_SIG: "The wallet's signature didn't match. Please try again.",
  BAD_PERMIT_SIG: "The wallet's token approval didn't match. Please try again.",
  BAD_DISPUTE_SIG: "Dispute with the wallet that made this payment.",
  POOL_INSUFFICIENT: "We can't sell that much right now. Try a smaller amount.",
  DAILY_CAP: "Today's buying limit has been reached. Try a smaller amount or come back tomorrow.",
  PAYMENT_PROVIDER: "Couldn't start the payment. Please try again.",
  CHAIN_UNAVAILABLE: "The network isn't responding. Try again in a moment.",
  UNKNOWN_ORDER: "We couldn't find that purchase.",
};

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(FRIENDLY[code] ?? message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      ...init,
      headers: { accept: "application/json", ...(init?.body ? { "content-type": "application/json" } : {}), ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "NETWORK", "Can't reach RupeeRail. Check your connection.");
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiError(res.status, body?.error ?? "HTTP_ERROR", body?.message ?? "Something went wrong. Please try again.");
  }
  return body as T;
}

const post = <T>(path: string, body: unknown) => call<T>(path, { method: "POST", body: JSON.stringify(body) });

export const api = {
  config: () => call<AppConfig>("/config"),
  account: (chain: ChainKey, address: string) =>
    call<Account>(`/account/${encodeURIComponent(chain)}/${encodeURIComponent(address)}`),
  quote: (chain: ChainKey, user: string, inr: string, qrText: string) =>
    post<Quote>("/quote", { chain, user, inr, qrText }),
  submit: (chain: ChainKey, intent: Record<string, string>, userSig: string, permit: { deadline: string; v: number; r: string; s: string }) =>
    post<{ id: string; txHash: string; explorer: string; status: string }>("/offramp", { chain, intent, userSig, permit }),
  offramp: (id: string, viewToken?: string) =>
    call<OfframpView>(`/offramp/${encodeURIComponent(id)}`, viewToken ? { headers: { "x-view-token": viewToken } } : undefined),
  history: (user: string) => call<{ items: OfframpView[] }>(`/offramps?user=${encodeURIComponent(user)}`),
  dispute: (chain: ChainKey, id: string, deadline: string, userSig: string) =>
    post<{ id: string; txHash: string; status: string }>("/dispute", { chain, id, deadline, userSig }),
  onramp: (chain: ChainKey, wallet: string, inr: string) => post<OnrampOrder>("/onramp", { chain, wallet, inr }),
  onrampOrder: (orderId: string) => call<OnrampOrder>(`/onramp/${encodeURIComponent(orderId)}`),
};
