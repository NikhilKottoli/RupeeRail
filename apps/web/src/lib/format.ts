import type { OfframpView } from "./api";

// Whole rupees stay short (₹25), and anything else always shows paise (₹25.50).
const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2, trailingZeroDisplay: "stripIfInteger" });

export function formatInr(paise: number | string | null | undefined): string {
  if (paise === null || paise === undefined) return "—";
  return INR.format(Number(paise) / 100);
}

export function shortAddress(address: string): string {
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}

export function formatUnits(value: string | null | undefined, decimals: number): string {
  if (!value) return "—";
  const padded = value.padStart(decimals + 1, "0");
  const whole = padded.slice(0, -decimals);
  const fraction = padded.slice(-decimals).replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole;
}

export function formatDateTime(unixSeconds: string | number): string {
  return new Date(Number(unixSeconds) * 1000).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export type Tone = "progress" | "success" | "warning" | "error" | "neutral";

export interface StatusText {
  title: string;
  detail: string;
  tone: Tone;
  /** Still changing without any user action, so keep polling. */
  active: boolean;
}

/**
 * COMPLETED is worded carefully: it means the operator reported the payout, which the chain can't verify.
 * That's why the user is told they can dispute, rather than told the merchant has the money.
 */
export function describeStatus(view: Pick<OfframpView, "status" | "payout" | "refundReasonName">): StatusText {
  switch (view.status) {
    case "QUOTED":
    case "SUBMITTED":
      return { title: "Sending to the network", detail: "Locking your funds. You don't pay any gas.", tone: "progress", active: true };
    case "LOCKED":
      if (view.payout === "NEEDS_OPERATOR") {
        return { title: "Being checked", detail: "We're confirming this payment. Your funds are safe in escrow.", tone: "warning", active: true };
      }
      return { title: "Paying the merchant", detail: "Your funds are locked in escrow while the INR is sent.", tone: "progress", active: true };
    case "COMPLETED":
      return { title: "Payment processed", detail: "If the merchant didn't get paid, you can dispute it below.", tone: "success", active: false };
    case "RELEASED":
      return { title: "Payment settled", detail: "The dispute window has ended.", tone: "success", active: false };
    case "REFUNDED":
      return { title: "Refunded", detail: refundDetail(view.refundReasonName), tone: "neutral", active: false };
    case "DISPUTED":
      return { title: "Under review", detail: "Your dispute is open and the funds are frozen. They come back to you if it isn't resolved within 7 days.", tone: "warning", active: true };
    case "REVERSED":
      return { title: "Returned to you", detail: "These funds were sent back to your wallet.", tone: "neutral", active: false };
    default:
      return { title: view.status, detail: "", tone: "neutral", active: true };
  }
}

function refundDetail(reason: string | null): string {
  switch (reason) {
    case "INVALID_QR":
      return "The QR code couldn't be paid. Your funds were returned.";
    case "AMOUNT_MISMATCH":
      return "The amount didn't match the QR code. Your funds were returned.";
    case "DISPATCH_FAILED":
      return "The merchant couldn't be paid. Your funds were returned.";
    case "USER_RECLAIM":
      return "The payment wasn't completed in time. Your funds were returned.";
    default:
      return "Your funds were returned to your wallet.";
  }
}
