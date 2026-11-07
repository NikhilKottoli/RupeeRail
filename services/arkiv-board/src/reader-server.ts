/**
 * The public read service behind /arkiv on the deployed site. It holds no signing key, only an Arkiv access key, which
 * must not reach a browser. It answers a fixed set of bounded questions and is never a general RPC proxy, and a failure
 * upstream is reported as a failure, never as an empty list.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { loadEnv, makeReader, makeWatcher } from "./arkiv.ts";
import { CATEGORIES, OUTCOMES } from "./ops.ts";
import { allReceipts, findCard, foreignCards, listCards, listReceipts, queryText, statsFrom, workingWallets, type CardRow, type ReceiptFilter, type Reader, type ReceiptRow } from "./reads.ts";
import { CARD_DAYS, LIVE_MINUTES, PUBLISHER, RECEIPT_DAYS } from "./schema.ts";

const PORT = 8792;
const CACHE_MS = 5_000;
const MAX_CACHE = 200;
const RATE_PER_MINUTE = 240;
const MAX_FEEDS = 200;
const PING_MS = 25_000;
const RESTART_WATCH_MS = 5_000;
const PAGE = 20;
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;

class BadRequest extends Error {}
class NotFound extends Error {}

export function parseReceiptQuery(params: URLSearchParams): ReceiptFilter {
  const known = new Set(["outcome", "minInr", "maxInr", "days", "before"]);
  for (const key of params.keys()) if (!known.has(key)) throw new BadRequest(`unknown filter: ${key}`);
  const filter: ReceiptFilter = { limit: PAGE };
  const whole = (name: string, max: number): number | undefined => {
    const text = params.get(name);
    if (text === null || text === "") return undefined;
    if (!/^\d{1,15}$/.test(text) || Number(text) > max) throw new BadRequest(`${name} isn't a sensible number`);
    return Number(text);
  };
  const outcome = params.get("outcome");
  if (outcome) {
    if (!OUTCOMES.includes(outcome as never)) throw new BadRequest("unknown outcome");
    filter.outcome = outcome;
  }
  const min = whole("minInr", 10_000_000);
  const max = whole("maxInr", 10_000_000);
  if (min !== undefined) filter.minInrPaise = min * 100;
  if (max !== undefined) filter.maxInrPaise = max * 100;
  const days = whole("days", 365);
  if (days !== undefined) filter.since = Date.now() - days * 86_400_000;
  const before = whole("before", Number.MAX_SAFE_INTEGER);
  if (before !== undefined) filter.before = before;
  return filter;
}

export interface Service {
  meta(): Promise<unknown>;
  agents(category?: string): Promise<unknown>;
  agent(wallet: string): Promise<unknown>;
  receipts(wallet: string, filter: ReceiptFilter): Promise<unknown>;
}

function withStats(card: CardRow, receipts: ReceiptRow[], working: Set<string>) {
  const mine = receipts.filter((r) => r.wallet.toLowerCase() === card.wallet.toLowerCase());
  return { ...card, stats: statsFrom(mine), working: working.has(card.wallet.toLowerCase()) };
}

export function makeService(reader: Reader, watching: () => boolean): Service {
  return {
    async meta() {
      return { publisher: PUBLISHER, chainId: 7794095, explorer: "https://rabdi.explorer.arkiv.network", lifetimes: { cardDays: CARD_DAYS, receiptDays: RECEIPT_DAYS, liveMinutes: LIVE_MINUTES }, categories: CATEGORIES, foreignCards: await foreignCards(reader), queries: queryText(), live: watching() };
    },
    async agents(category) {
      const [cards, receipts, working] = await Promise.all([listCards(reader, category), allReceipts(reader), workingWallets(reader)]);
      return { agents: cards.map((card) => withStats(card, receipts, working)) };
    },
    async agent(wallet) {
      const card = await findCard(reader, wallet);
      if (!card) throw new NotFound("no public record for this wallet");
      const [receipts, working] = await Promise.all([allReceipts(reader, wallet), workingWallets(reader)]);
      return withStats(card, receipts, working);
    },
    async receipts(wallet, filter) {
      const items = await listReceipts(reader, { ...filter, wallet });
      return { items, next: items.length === PAGE ? items[items.length - 1].finishedAt : null };
    },
  };
}

function reply(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", "x-content-type-options": "nosniff" });
  response.end(JSON.stringify(body));
}

export interface ReaderServer {
  server: Server;
  /** Forgets cached answers and tells every open page to refetch. */
  changed(reason: string): void;
}

export function makeReaderServer(service: Service): ReaderServer {
  const cache = new Map<string, { at: number; value: unknown }>();
  const feeds = new Set<ServerResponse>();
  const hits = new Map<string, { minute: number; count: number }>();

  const cached = async (key: string, read: () => Promise<unknown>): Promise<unknown> => {
    const hit = cache.get(key);
    if (hit && Date.now() - hit.at < CACHE_MS) return hit.value;
    const value = await read();
    if (cache.size >= MAX_CACHE) cache.clear();
    cache.set(key, { at: Date.now(), value });
    return value;
  };

  const limited = (request: IncomingMessage): boolean => {
    // Caddy appends the client it saw, so the last entry is the one that can't be forged.
    const forwarded = String(request.headers["x-forwarded-for"] ?? "").split(",").pop()?.trim();
    const who = forwarded || request.socket.remoteAddress || "unknown";
    const minute = Math.floor(Date.now() / 60_000);
    const seen = hits.get(who);
    if (!seen || seen.minute !== minute) {
      if (hits.size > 10_000) hits.clear();
      hits.set(who, { minute, count: 1 });
      return false;
    }
    seen.count += 1;
    return seen.count > RATE_PER_MINUTE;
  };

  const openFeed = (response: ServerResponse): void => {
    if (feeds.size >= MAX_FEEDS) return reply(response, 503, { error: "too many live connections" });
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-store", connection: "keep-alive", "x-accel-buffering": "no" });
    response.write("retry: 5000\n\nevent: ready\ndata: {}\n\n");
    feeds.add(response);
    const ping = setInterval(() => response.write(": ping\n\n"), PING_MS);
    response.on("close", () => {
      clearInterval(ping);
      feeds.delete(response);
    });
  };

  const server = createServer(async (request, response) => {
    try {
      if (request.method !== "GET") return reply(response, 405, { error: "this service only reads" });
      if (limited(request)) return reply(response, 429, { error: "too many requests" });
      const url = new URL(request.url ?? "/", "http://localhost");
      const parts = url.pathname.split("/").filter(Boolean);
      if (parts[0] !== "arkiv") throw new NotFound("not found");
      if (parts[1] === "health" && parts.length === 2) return reply(response, 200, { ok: true });
      if (parts[1] === "feed" && parts.length === 2) return openFeed(response);
      if (parts[1] === "meta" && parts.length === 2) return reply(response, 200, await cached("meta", () => service.meta()));
      if (parts[1] === "agents" && parts.length === 2) {
        const category = url.searchParams.get("category") || undefined;
        if (category && !CATEGORIES.includes(category as never)) throw new BadRequest("unknown category");
        return reply(response, 200, await cached(`agents:${category ?? ""}`, () => service.agents(category)));
      }
      if (parts[1] === "agents" && parts.length >= 3) {
        const wallet = parts[2];
        if (!ADDRESS.test(wallet)) throw new BadRequest("that isn't a wallet address");
        if (parts.length === 3) return reply(response, 200, await cached(`agent:${wallet.toLowerCase()}`, () => service.agent(wallet)));
        if (parts.length === 4 && parts[3] === "receipts") {
          const filter = parseReceiptQuery(url.searchParams);
          return reply(response, 200, await cached(`receipts:${wallet.toLowerCase()}:${url.search}`, () => service.receipts(wallet, filter)));
        }
      }
      throw new NotFound("not found");
    } catch (error) {
      if (error instanceof BadRequest) return reply(response, 400, { error: error.message });
      if (error instanceof NotFound) return reply(response, 404, { error: error.message });
      // SDK errors can contain the RPC URL, which contains the access key, so only the type is logged.
      console.error(`read failed: ${error instanceof Error ? error.name : "unknown"}`);
      return reply(response, 502, { error: "Arkiv couldn't be read just now" });
    }
  });

  return {
    server,
    changed(reason) {
      cache.clear();
      for (const feed of feeds) feed.write(`event: changed\ndata: ${JSON.stringify({ reason })}\n\n`);
    },
  };
}

/** Watches Arkiv over a WebSocket and starts again after any error, then reconciles, because a missed event is never replayed. */
function watch(accessKey: string, changed: (reason: string) => void, setLive: (live: boolean) => void): void {
  const publisher = PUBLISHER.toLowerCase();
  const start = (): void => {
    let stop: (() => void) | undefined;
    let restarting = false;
    const restart = (): void => {
      if (restarting) return;
      restarting = true;
      setLive(false);
      try {
        stop?.();
      } catch {
        // It's already gone.
      }
      setTimeout(start, RESTART_WATCH_MS);
    };
    try {
      stop = makeWatcher(accessKey).watchEntityEvents({
        onEvent: (event) => {
          const owners = [(event as { owner?: string }).owner, (event as { newOwner?: string }).newOwner, (event as { previousOwner?: string }).previousOwner];
          // Rabdi is shared, so other projects' events are ignored without any lookup.
          if (owners.some((owner) => owner?.toLowerCase() === publisher)) changed(event.type);
        },
        onError: () => {
          console.error("the Arkiv event watcher failed; restarting it");
          restart();
          changed("reconnected");
        },
      });
      setLive(true);
    } catch {
      restart();
    }
  };
  start();
}

async function main(): Promise<void> {
  const env = loadEnv();
  let live = false;
  const { server, changed } = makeReaderServer(makeService(makeReader(env.ARKIV_ACCESS_KEY), () => live));
  watch(env.ARKIV_ACCESS_KEY, changed, (value) => { live = value; });
  server.listen(PORT, "127.0.0.1", () => console.log(`reader listening on 127.0.0.1:${PORT}`));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error && error.message.startsWith("ARKIV_ACCESS_KEY") ? error.message : "the reader couldn't start");
    process.exit(1);
  });
}
