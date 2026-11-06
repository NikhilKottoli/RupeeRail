/**
 * Reads a UPI QR for display. The backend parses it again and has the final say.
 * Mirrors services/api-py/app/qr.py.
 */

export interface ParsedQr {
  qrText: string;
  upiId: string;
  name: string | null;
  amountPaise: number | null;
}

export class QrError extends Error {}

const VPA_RE = /^[a-zA-Z0-9._-]{2,256}@[a-zA-Z0-9]{2,64}$/;
const AMOUNT_RE = /^(\d+)(?:\.(\d{1,2}))?$/;
const MAX_QR_BYTES = 512;

function decode(value: string): string {
  try {
    return decodeURIComponent(value.replace(/\+/g, " "));
  } catch {
    return value;
  }
}

export function parseUpiQr(raw: string): ParsedQr {
  const text = raw.trim();
  if (!text) throw new QrError("That QR code is empty.");
  if (new TextEncoder().encode(raw).length > MAX_QR_BYTES) throw new QrError("That QR code is too long.");
  if (text.startsWith("000201")) throw new QrError("This merchant QR format isn't supported yet.");
  const match = /^upi:\/\/pay\?(.*)$/is.exec(text);
  if (!match) throw new QrError("That isn't a UPI payment QR.");

  const params = new Map<string, string>();
  for (const part of match[1].split("&")) {
    if (!part) continue;
    const index = part.indexOf("=");
    const key = (index < 0 ? part : part.slice(0, index)).toLowerCase();
    if (!params.has(key)) params.set(key, index < 0 ? "" : decode(part.slice(index + 1))); // first occurrence wins
  }

  const upiId = params.get("pa");
  if (!upiId || !VPA_RE.test(upiId)) throw new QrError("This QR doesn't have a valid UPI ID.");
  const currency = params.get("cu");
  if (currency !== undefined && currency.toUpperCase() !== "INR") throw new QrError("Only INR payments are supported.");

  let amountPaise: number | null = null;
  const am = params.get("am");
  if (am) {
    const m = AMOUNT_RE.exec(am);
    if (!m) throw new QrError("This QR has an invalid amount.");
    amountPaise = Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0"));
  }
  return { qrText: raw, upiId, name: params.get("pn") || null, amountPaise };
}

/** Rupees typed by the user, as paise. Null if it isn't a valid amount. */
export function rupeesToPaise(input: string): number | null {
  const m = AMOUNT_RE.exec(input.trim());
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0"));
}

export function paiseToRupeesString(paise: number): string {
  const cents = paise % 100;
  return cents === 0 ? String(Math.floor(paise / 100)) : `${Math.floor(paise / 100)}.${String(cents).padStart(2, "0")}`;
}
