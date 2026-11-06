/**
 * Buying tokens with INR (the onramp). The user pays on Razorpay's hosted page, which unloads this app, so
 * an order is remembered here before leaving and recognised again from the return URL:
 *
 *   /?onramp=<orderId>&payment=received     (the backend's callback redirect; "unverified" if the signature failed)
 *
 * The return is a query on "/", never a path: /onramp/... belongs to the API on this hostname.
 */

import type { OnrampOrder } from "./api";
import type { StatusText } from "./format";

const ORDER_ID = /^0x[0-9a-fA-F]{64}$/;

export interface OnrampReturn {
  orderId: string;
  /** "received": Razorpay's signed redirect checked out. Tokens still wait for the server to confirm. */
  payment: "received" | "unverified" | null;
}

export function parseOnrampReturn(search: string): OnrampReturn | null {
  const params = new URLSearchParams(search);
  const orderId = params.get("onramp");
  if (!orderId || !ORDER_ID.test(orderId)) return null;
  const payment = params.get("payment");
  return {
    orderId: orderId.toLowerCase(),
    payment: payment === "received" || payment === "unverified" ? payment : null,
  };
}

/** Razorpay's redirect came back with a signature that didn't check out, so the backend names no order. */
export function isUnverifiedReturn(search: string): boolean {
  return parseOnrampReturn(search) === null && new URLSearchParams(search).get("payment") === "unverified";
}

/** The same URL without the onramp return parameters, so a reload doesn't show it again. */
export function stripOnrampReturn(href: string): string {
  const url = new URL(href);
  url.searchParams.delete("onramp");
  url.searchParams.delete("payment");
  return url.pathname + (url.searchParams.size ? `?${url.searchParams}` : "") + url.hash;
}

export function describeOnramp(order: Pick<OnrampOrder, "status" | "amount" | "symbol" | "checkout">,
                               justPaid = false): StatusText {
  const tokens = `${order.amount} ${order.symbol ?? ""}`.trim();
  switch (order.status) {
    case "AWAITING_FIAT":
      return justPaid
        ? { title: "Payment received", detail: "Confirming it with Razorpay. This takes a few seconds.", tone: "progress", active: true }
        : {
            title: "Waiting for your payment",
            detail: order.checkout ? "Finish paying on Razorpay. Your tokens are sent as soon as it's confirmed." : "Your tokens are sent once the payment is confirmed.",
            tone: "progress",
            active: true,
          };
    case "PAYING":
      return { title: `Sending ${tokens}`, detail: "Your payment is confirmed. The tokens are on their way to your wallet.", tone: "progress", active: true };
    case "PAID":
      return { title: `${tokens} sent`, detail: "The tokens are in your wallet.", tone: "success", active: false };
    case "EXPIRED":
      return { title: "Payment link expired", detail: "Nothing was charged. Start again to get a new rate.", tone: "neutral", active: false };
    case "FAILED":
      return { title: "Purchase failed", detail: "Something went wrong. If you were charged, contact support with the receipt below.", tone: "error", active: false };
    default:
      return { title: order.status, detail: "", tone: "neutral", active: true };
  }
}

// ---- purchases this browser started ---------------------------------------------------------

export interface LocalOnramp {
  orderId: string;
  chain: string;
  wallet: string;
  createdAt: number;
}

const KEY = "rupeerail.onramps";
const KEEP = 10;

export function recentOnramps(wallet?: string): LocalOnramp[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    const list = Array.isArray(parsed) ? (parsed as LocalOnramp[]) : [];
    return wallet ? list.filter((o) => o.wallet?.toLowerCase() === wallet.toLowerCase()) : list;
  } catch {
    return [];
  }
}

export function rememberOnramp(order: LocalOnramp): void {
  try {
    const rest = recentOnramps().filter((o) => o.orderId.toLowerCase() !== order.orderId.toLowerCase());
    localStorage.setItem(KEY, JSON.stringify([order, ...rest].slice(0, KEEP)));
  } catch {
    // Storage can be full or blocked. The return URL still carries the order id.
  }
}
