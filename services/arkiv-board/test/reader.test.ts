import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import { test } from "node:test";
import { allReceipts, foreignCards, listCards, listReceipts, statsFrom, type ReceiptRow } from "../src/reads.ts";
import { makeReaderServer, makeService, parseReceiptQuery, type Service } from "../src/reader-server.ts";
import { PUBLISHER } from "../src/schema.ts";

const WALLET = `0x${"a".repeat(40)}`;

function row(outcome: string, inrPaise: string, finishedAt = 1): ReceiptRow {
  return { key: "k", wallet: WALLET, offrampId: "o", outcome, inrPaise, finishedAt, chain: "arc", lockTx: "t", creator: PUBLISHER, expiresAt: "" };
}

test("totals count only what was paid", () => {
  assert.deepEqual(statsFrom([row("paid", "23200", 9), row("not_paid", "100", 7), row("unknown", "500", 8), row("paid", "100", 3)]),
    { paid: 2, notPaid: 1, unknown: 1, paidInrPaise: "23300", lastPaidAt: 9 });
});

test("filters are limited to known names and sensible numbers", () => {
  const filter = parseReceiptQuery(new URLSearchParams("outcome=paid&minInr=100&maxInr=500&before=99"));
  assert.equal(filter.outcome, "paid");
  assert.equal(filter.minInrPaise, 10000);
  assert.equal(filter.maxInrPaise, 50000);
  assert.equal(filter.before, 99);
  for (const bad of ["evil=1", "outcome=maybe", "minInr=-1", "minInr=1e5", "days=9999", "maxInr=abc"]) assert.throws(() => parseReceiptQuery(new URLSearchParams(bad)));
});

/** Records what each query asked for. */
function recordingReader(rows: unknown[] = [], counts: bigint[] = []) {
  const calls: { creator?: string; where: unknown[] }[] = [];
  const reader = {
    getBlockNumber: async () => 100n,
    select: () => {
      const call: { creator?: string; where: unknown[] } = { where: [] };
      calls.push(call);
      const query = {
        from: () => query,
        createdBy: (who: string) => { call.creator = who; return query; },
        where: (w: unknown) => { call.where.push(w); return query; },
        orderBy: () => query, limit: () => query,
        fetch: async () => ({ entities: rows }),
      };
      return query;
    },
    count: () => {
      const query = { from: () => query, createdBy: () => query, fetch: async () => counts.shift()! };
      return query;
    },
  };
  return { reader, calls };
}

test("every query filters on the publisher, so someone else's entity can't be shown", async () => {
  const { reader, calls } = recordingReader();
  await listCards(reader);
  await listCards(reader, "food");
  await listReceipts(reader, { wallet: WALLET, outcome: "paid", minInrPaise: 1 });
  await allReceipts(reader);
  assert.ok(calls.length >= 4);
  for (const call of calls) assert.equal(call.creator, PUBLISHER);
});

test("cards from other wallets are counted and left out", async () => {
  const { reader } = recordingReader([], [5n, 2n]);
  assert.equal(await foreignCards(reader), 3);
});

const CARD = { key: "k", wallet: WALLET, category: "food", name: "Butler", avatar: "chef", colour: "saffron", creator: PUBLISHER, expiresAtBlock: "1", expiresAt: "", stats: {}, working: false };

function service(overrides: Partial<Service> = {}): Service & { calls: number } {
  const s = {
    calls: 0,
    meta: async () => ({ publisher: PUBLISHER }),
    agents: async () => { s.calls += 1; return { agents: [CARD] }; },
    agent: async () => CARD,
    receipts: async () => ({ items: [], next: null }),
    ...overrides,
  };
  return s;
}

async function serve(t: { after(fn: () => void): void }, s: Service) {
  const made = makeReaderServer(s);
  await new Promise<void>((resolve) => made.server.listen(0, "127.0.0.1", resolve));
  t.after(() => { made.server.closeAllConnections(); made.server.close(); });
  return { ...made, base: `http://127.0.0.1:${(made.server.address() as AddressInfo).port}` };
}

test("the reader only answers its fixed questions", async (t) => {
  const { base } = await serve(t, service());
  assert.equal((await fetch(`${base}/arkiv/agents`)).status, 200);
  assert.equal((await fetch(`${base}/arkiv/agents/${WALLET}`)).status, 200);
  assert.equal((await fetch(`${base}/arkiv/agents/${WALLET}/receipts?outcome=paid`)).status, 200);
  assert.equal((await fetch(`${base}/arkiv/meta`)).status, 200);
  assert.equal((await fetch(`${base}/arkiv/agents?category=nope`)).status, 400);
  assert.equal((await fetch(`${base}/arkiv/agents/not-a-wallet`)).status, 400);
  assert.equal((await fetch(`${base}/arkiv/agents/${WALLET}/receipts?sql=drop`)).status, 400);
  assert.equal((await fetch(`${base}/arkiv/rpc`)).status, 404);
  assert.equal((await fetch(`${base}/other`)).status, 404);
  assert.equal((await fetch(`${base}/arkiv/agents`, { method: "POST", body: "{}" })).status, 405);
});

test("an Arkiv failure is a failure, never an empty list, and doesn't leak the key", async (t) => {
  const { base } = await serve(t, service({ agents: async () => { throw new Error("https://rpc.example/secret-access-key"); } }));
  const response = await fetch(`${base}/arkiv/agents`);
  assert.equal(response.status, 502);
  assert.ok(!(await response.text()).includes("secret"));
});

test("a wallet with no card is not found", async (t) => {
  const { reader } = recordingReader();
  const { base } = await serve(t, makeService(reader, () => true));
  assert.equal((await fetch(`${base}/arkiv/agents/${WALLET}`)).status, 404);
});

test("answers are cached until something changes, and open pages are told", async (t) => {
  const s = service();
  const { base, changed } = await serve(t, s);
  const controller = new AbortController();
  t.after(() => controller.abort());
  const feed = await fetch(`${base}/arkiv/feed`, { signal: controller.signal });
  assert.equal(feed.headers.get("content-type"), "text/event-stream");
  const stream = feed.body!.getReader();
  const decoder = new TextDecoder();
  assert.match(decoder.decode((await stream.read()).value), /event: ready/);

  await fetch(`${base}/arkiv/agents`);
  await fetch(`${base}/arkiv/agents`);
  assert.equal(s.calls, 1);
  changed("EntityCreated");
  assert.match(decoder.decode((await stream.read()).value), /event: changed/);
  await fetch(`${base}/arkiv/agents`);
  assert.equal(s.calls, 2);
});
