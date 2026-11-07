import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { Op } from "../src/ops.ts";
import { InvalidOp } from "../src/ops.ts";
import { Journal, NeedsReconcile, Publisher } from "../src/publish.ts";
import { makeServer } from "../src/publisher-server.ts";

const WALLET = `0x${"a".repeat(40)}`;
const HASH = `0x${"b".repeat(64)}`;
const card = { wallet: WALLET, category: "food", name: "Butler", avatar: "chef", colour: "saffron" };
const op: Op = { id: "r1", kind: "run_finished", card, receipt: { offrampId: HASH, outcome: "paid", inrPaise: 23200, finishedAt: 5, chain: "arc", lockTx: HASH } };

/** A fake Arkiv: rows per schema name, which executeBatch fills in when it's told to apply. */
function fake(behaviour: { send?: "ok" | "throw" | "applied-then-throw"; receipt?: string } = {}) {
  const rows: Record<string, unknown[]> = { "rupeerail.agent": [], "rupeerail.live": [], "rupeerail.receipt": [] };
  const sends: unknown[] = [];
  const builder = (name: string) => {
    const query = {
      createdBy: () => query, where: () => query, limit: () => query, orderBy: () => query,
      fetch: async () => ({ entities: rows[name] }),
    };
    return query;
  };
  const reader = {
    select: () => ({ from: (schema: { name?: string }) => builder(schema.name ?? "") }),
    getTransactionReceipt: async () => ({ status: behaviour.receipt ?? "success" }),
  };
  const apply = (plan: { creates: { schema: { name?: string } }[] }) => {
    for (const create of plan.creates) rows[create.schema.name ?? ""].push({ $key: HASH, wallet: { value: WALLET }, category: { value: "food" }, name: { value: "Butler" }, avatar: { value: "chef" }, colour: { value: "saffron" } });
  };
  const wallet = {
    executeBatch: async (plan: never) => {
      sends.push(plan);
      if (behaviour.send === "throw") throw new Error("the response was lost");
      apply(plan);
      if (behaviour.send === "applied-then-throw") throw new Error("the response was lost");
      return { txHash: HASH };
    },
  };
  return { reader, wallet, sends, rows };
}

function publisher(f: ReturnType<typeof fake>, journalPath = join(mkdtempSync(join(tmpdir(), "arkiv-")), "journal.jsonl")) {
  const journal = new Journal(journalPath);
  const estimated: string[] = [];
  return {
    journal, journalPath, estimated,
    publisher: new Publisher({
      reader: f.reader, wallet: f.wallet, address: WALLET as `0x${string}`, journal,
      estimate: async () => { estimated.push("estimate"); return 1000n; },
      txParams: async () => ({ nonce: 1, gas: 2_000_000n, gasPrice: 1n }),
    }),
  };
}

test("a write is journaled before it's sent, and only once", async () => {
  const f = fake();
  const p = publisher(f);
  const result = await p.publisher.apply(op);
  assert.equal(result.state, "done");
  assert.equal(result.txHash, HASH);
  assert.equal(f.sends.length, 1);
  assert.deepEqual(p.journal.entries().map((e) => e.state), ["intent", "sent", "verified"]);
  assert.deepEqual(p.journal.unresolved(), []);
});

test("the same request again sends nothing", async () => {
  const f = fake();
  const p = publisher(f);
  await p.publisher.apply(op);
  const again = await p.publisher.apply(op);
  assert.equal(f.sends.length, 1);
  assert.equal(again.alreadyApplied, true);
});

test("a failed estimate signs nothing and can be tried again", async () => {
  const f = fake();
  const journalPath = join(mkdtempSync(join(tmpdir(), "arkiv-")), "journal.jsonl");
  const journal = new Journal(journalPath);
  const broken = new Publisher({
    reader: f.reader, wallet: f.wallet, address: WALLET as `0x${string}`, journal,
    estimate: async () => { throw new Error("no estimate"); },
    txParams: async () => ({}),
  });
  await assert.rejects(broken.apply(op), /no estimate/);
  assert.equal(f.sends.length, 0);
  assert.deepEqual(journal.entries(), []);
});

test("a lost response whose write landed counts as done, with no second send", async () => {
  const f = fake({ send: "applied-then-throw" });
  const p = publisher(f);
  assert.equal((await p.publisher.apply(op)).state, "done");
  assert.equal(f.sends.length, 1);
  assert.deepEqual(p.journal.unresolved(), []);
});

test("an uncertain write is never resent, and stops every later write", async () => {
  const f = fake({ send: "throw" });
  const p = publisher(f);
  await assert.rejects(p.publisher.apply(op), NeedsReconcile);
  await assert.rejects(p.publisher.apply(op), NeedsReconcile);
  await assert.rejects(p.publisher.apply({ id: "c2", kind: "card", card }), NeedsReconcile);
  assert.equal(f.sends.length, 1);
  assert.deepEqual(p.journal.unresolved().map((e) => e.opId), ["r1"]);
});

test("after a restart, an uncertain write is still blocked until it's closed", async () => {
  const f = fake({ send: "throw" });
  const first = publisher(f);
  await assert.rejects(first.publisher.apply(op), NeedsReconcile);
  const restarted = publisher(f, first.journalPath);
  await assert.rejects(restarted.publisher.apply({ id: "c2", kind: "card", card }), NeedsReconcile);
  assert.equal(f.sends.length, 1);
  restarted.journal.add({ opId: "r1", state: "abandoned" });
  assert.deepEqual(restarted.journal.unresolved(), []);
});

test("a write found on Arkiv after a restart closes itself", async () => {
  const f = fake({ send: "throw" });
  const first = publisher(f);
  await assert.rejects(first.publisher.apply(op), NeedsReconcile);
  f.rows["rupeerail.receipt"].push({ $key: HASH });
  const again = fake();
  again.rows["rupeerail.receipt"].push({ $key: HASH });
  const restarted = publisher(again, first.journalPath);
  assert.equal((await restarted.publisher.apply(op)).alreadyApplied, true);
  assert.deepEqual(restarted.journal.unresolved(), []);
});

test("a reverted write isn't treated as done", async () => {
  const f = fake({ receipt: "reverted" });
  const p = publisher(f);
  f.wallet.executeBatch = async (plan: never) => { f.sends.push(plan); return { txHash: HASH }; };
  await assert.rejects(p.publisher.apply(op), NeedsReconcile);
  assert.equal(p.journal.entries().at(-1)?.txHash, HASH);
});

test("the journal holds no more than the public op", async () => {
  const f = fake();
  const p = publisher(f);
  await p.publisher.apply(op);
  const text = readFileSync(p.journalPath, "utf8");
  for (const word of ["prompt", "profile", "instructions", "privateKey"]) assert.ok(!text.includes(word));
});

async function call(port: number, init: RequestInit & { path?: string } = {}) {
  const response = await fetch(`http://127.0.0.1:${port}${init.path ?? "/ops"}`, { method: "POST", ...init });
  return { status: response.status, body: await response.json() as Record<string, unknown> };
}

test("the publisher refuses browsers, wrong tokens and fields it doesn't know", async (t) => {
  const applied: Op[] = [];
  const server = makeServer("right", async (o) => { applied.push(o); return { state: "done" }; }, () => []);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const port = (server.address() as AddressInfo).port;
  const good = { "content-type": "application/json", "x-publisher-token": "right" };
  const body = JSON.stringify(op);

  assert.equal((await call(port, { headers: { ...good, origin: "https://evil.example" }, body })).status, 403);
  assert.equal((await call(port, { headers: { ...good, "x-publisher-token": "wrong" }, body })).status, 401);
  assert.equal((await call(port, { headers: { "x-publisher-token": "right", "content-type": "text/plain" }, body })).status, 415);
  assert.equal((await call(port, { headers: good, body: "{nope" })).status, 400);
  assert.equal((await call(port, { headers: good, body: JSON.stringify({ ...op, prompt: "order dinner" }) })).status, 400);
  assert.equal((await call(port, { headers: good, body: "x".repeat(9000) })).status, 400);
  assert.equal(applied.length, 0);

  assert.equal((await call(port, { headers: good, body })).status, 200);
  assert.equal(applied.length, 1);
});

test("an uncertain write answers 409 and any other failure 503, with no details", async (t) => {
  let failure: Error = new NeedsReconcile("r1");
  const server = makeServer("right", async () => { throw failure; }, () => []);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const port = (server.address() as AddressInfo).port;
  const headers = { "content-type": "application/json", "x-publisher-token": "right" };
  assert.equal((await call(port, { headers, body: JSON.stringify(op) })).status, 409);
  failure = new Error("https://rpc.example/secret-access-key");
  const answer = await call(port, { headers, body: JSON.stringify(op) });
  assert.equal(answer.status, 503);
  assert.ok(!JSON.stringify(answer.body).includes("secret"));
  assert.ok(new InvalidOp("x") instanceof Error);
});
