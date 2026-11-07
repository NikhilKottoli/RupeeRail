/** The public track records, read from Arkiv through the read-only service at /arkiv. They need no wallet. */

export type Outcome = "paid" | "not_paid" | "unknown";

export interface AgentStats { paid: number; notPaid: number; unknown: number; paidInrPaise: string; lastPaidAt: number | null }

export interface PublicAgent {
  key: string; wallet: string; category: string; name: string; avatar: string; colour: string; creator: string;
  expiresAt: string; stats: AgentStats; working: boolean;
}

export interface PublicReceipt {
  key: string; wallet: string; offrampId: string; outcome: Outcome; inrPaise: string; finishedAt: number; chain: string; lockTx: string; creator: string; expiresAt: string;
}

export interface ArkivMeta {
  publisher: string; explorer: string; categories: string[]; foreignCards: number; queries: Record<string, string>;
  lifetimes: { cardDays: number; receiptDays: number; liveMinutes: number };
}

export interface ReceiptFilters { outcome?: Outcome; minInr?: number; days?: number; before?: number }

export class ArkivError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function read<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/arkiv/${path}`, { headers: { accept: "application/json" } });
  } catch {
    throw new ArkivError(0, "Can't reach RupeeRail. Check your connection.");
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { error?: string } | null;
    throw new ArkivError(response.status, body?.error ?? "Arkiv couldn't be read just now.");
  }
  return (await response.json()) as T;
}

export const arkiv = {
  meta: () => read<ArkivMeta>("meta"),
  agents: (category?: string) => read<{ agents: PublicAgent[] }>(`agents${category ? `?category=${encodeURIComponent(category)}` : ""}`),
  agent: (wallet: string) => read<PublicAgent>(`agents/${encodeURIComponent(wallet)}`),
  receipts: (wallet: string, filters: ReceiptFilters) => {
    const query = new URLSearchParams();
    if (filters.outcome) query.set("outcome", filters.outcome);
    if (filters.minInr) query.set("minInr", String(filters.minInr));
    if (filters.days) query.set("days", String(filters.days));
    if (filters.before) query.set("before", String(filters.before));
    const text = query.toString();
    return read<{ items: PublicReceipt[]; next: number | null }>(`agents/${encodeURIComponent(wallet)}/receipts${text ? `?${text}` : ""}`);
  },
};

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const HASH = /^0x[0-9a-fA-F]{64}$/;

/** The wallet in an /agents/<wallet> address, or null for the directory. Undefined for any other page. */
export function parseAgentsPath(pathname: string): string | null | undefined {
  const match = /^\/agents(?:\/([^/]+))?\/?$/.exec(pathname);
  if (!match) return undefined;
  return match[1] && ADDRESS.test(match[1]) ? match[1] : null;
}

export function arcTxUrl(receipt: Pick<PublicReceipt, "chain" | "lockTx">): string | null {
  return receipt.chain === "arc" && HASH.test(receipt.lockTx) ? `https://explorer.testnet.arc.io/tx/${receipt.lockTx}` : null;
}

export function arcAddressUrl(address: string): string | null {
  return ADDRESS.test(address) ? `https://explorer.testnet.arc.io/address/${address}` : null;
}

export function entityUrl(explorer: string, key: string): string | null {
  return HASH.test(key) ? `${explorer}/entity/${key}` : null;
}

export function publisherUrl(meta: Pick<ArkivMeta, "explorer" | "publisher">): string | null {
  return ADDRESS.test(meta.publisher) ? `${meta.explorer}/address/${meta.publisher}` : null;
}

// The avatars are copied from the extension. Anything else falls back, so an entity can't choose what image loads.
const AVATARS = new Set(["chef", "shopper", "traveller", "gardener", "student", "doctor", "artist", "musician", "athlete", "mechanic", "scientist", "robot"]);
const COLOURS: Record<string, string> = {
  saffron: "#fbd38d", coral: "#fbb4a0", rose: "#f7b6cf", violet: "#cdbcf2", indigo: "#b5c2f5", teal: "#a2e1da", leaf: "#bfe6a8", slate: "#c4cfd8",
};

export function avatarUrl(avatar: string): string {
  return `/avatars/${AVATARS.has(avatar) ? avatar : "robot"}.svg`;
}

export function avatarColour(colour: string): string {
  return COLOURS[colour] ?? COLOURS.slate;
}

const CATEGORY_NAMES: Record<string, string> = { food: "Food", groceries: "Groceries", shopping: "Shopping", travel: "Travel", bills: "Bills", other: "Other" };

export function categoryName(category: string): string {
  return CATEGORY_NAMES[category] ?? "Other";
}
