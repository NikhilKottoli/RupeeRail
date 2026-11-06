/**
 * The backend only reveals a payee to whoever holds the quote's viewToken, so each browser keeps its own
 * tokens to show payee names in history. Other devices see the same payments, but without names.
 */

export interface LocalPayment {
  viewToken: string;
  upiId: string;
  name: string | null;
  createdAt: number;
}

const key = (address: string) => `rupeerail.payments.${address.toLowerCase()}`;

function load(address: string): Record<string, LocalPayment> {
  try {
    const parsed = JSON.parse(localStorage.getItem(key(address)) ?? "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function rememberPayment(address: string, id: string, payment: LocalPayment): void {
  try {
    localStorage.setItem(key(address), JSON.stringify({ ...load(address), [id.toLowerCase()]: payment }));
  } catch {
    // Storage can be full or blocked. History then shows the payment without its payee name.
  }
}

export function localPayments(address: string): Record<string, LocalPayment> {
  return load(address);
}
