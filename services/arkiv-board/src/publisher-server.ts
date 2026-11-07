/**
 * The only thing that holds the publisher's key. It runs beside the agent server and listens on localhost:
 *
 *   npm run publisher
 *
 * A browser sends an Origin header that a page can't forge, so any request with one is refused, as the agent server
 * does. The token keeps out other local programs. Only validateOp's fields can ever reach Arkiv.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { join } from "node:path";
import { timingSafeEqual } from "node:crypto";
import { privateKeyToAccount } from "viem/accounts";
import { estimateWrite, loadEnv, makeClients, RABDI_CHAIN_ID, SERVICE_DIR, txParamsFor } from "./arkiv.ts";
import { InvalidOp, validateOp, type Op } from "./ops.ts";
import { Journal, NeedsReconcile, Publisher } from "./publish.ts";
import { PUBLISHER } from "./schema.ts";

const PORT = 8793;
const MAX_BODY = 8_192;
const MAX_COST_WEI = 10n ** 16n; // 0.01 test GLM for one write
const MAX_SPEND_WEI = 5n * 10n ** 17n; // 0.5 test GLM between restarts

function tokenMatches(given: string | string[] | undefined, expected: string): boolean {
  if (typeof given !== "string") return false;
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

async function readBody(request: IncomingMessage): Promise<string> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY) throw new InvalidOp("the request is too large");
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function reply(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  response.end(JSON.stringify(body));
}

export function makeServer(token: string, apply: (op: Op) => Promise<unknown>, unresolved: () => string[]): Server {
  return createServer(async (request, response) => {
    try {
      if (request.headers.origin !== undefined) return reply(response, 403, { error: "browsers may not use the publisher" });
      if (!tokenMatches(request.headers["x-publisher-token"], token)) return reply(response, 401, { error: "wrong token" });
      if (request.method === "GET" && request.url === "/status") return reply(response, 200, { ok: true, unresolved: unresolved() });
      if (request.method !== "POST" || request.url !== "/ops") return reply(response, 404, { error: "not found" });
      if (!request.headers["content-type"]?.startsWith("application/json")) return reply(response, 415, { error: "send JSON" });
      const op = validateOp(JSON.parse(await readBody(request)));
      return reply(response, 200, await apply(op));
    } catch (error) {
      if (error instanceof InvalidOp || error instanceof SyntaxError) return reply(response, 400, { error: error instanceof InvalidOp ? error.message : "that isn't JSON" });
      if (error instanceof NeedsReconcile) return reply(response, 409, { error: error.message });
      // SDK errors can contain the RPC URL, which contains the access key, so only the type is reported.
      console.error(`write not attempted or failed: ${error instanceof Error ? error.name : "unknown"}`);
      return reply(response, 503, { error: "Arkiv couldn't be reached; try again" });
    }
  });
}

async function main(): Promise<void> {
  const env = loadEnv();
  if (!env.PUBLISHER_TOKEN) throw new Error("PUBLISHER_TOKEN is not set");
  const account = privateKeyToAccount(env.BOARD_PUBLISHER_PRIVATE_KEY as `0x${string}`);
  if (account.address.toLowerCase() !== PUBLISHER.toLowerCase()) throw new Error("this key isn't the publisher's wallet");
  const { reader, wallet } = makeClients(account, env.ARKIV_ACCESS_KEY);
  if ((await reader.getChainId()) !== RABDI_CHAIN_ID) throw new Error("wrong chain");
  const journal = new Journal(join(SERVICE_DIR, "data", "publisher-journal.jsonl"));
  let spent = 0n;
  const publisher = new Publisher({
    reader, wallet, address: account.address, journal,
    estimate: (plan) => estimateWrite(account, env.ARKIV_ACCESS_KEY, (unsigned) => (unsigned as never as { executeBatch(p: unknown): Promise<unknown> }).executeBatch(plan)),
    txParams: async (estimate, operations) => {
      const params = await txParamsFor(reader, account.address, estimate, MAX_COST_WEI, operations);
      const cost = params.gas! * params.gasPrice!;
      if (spent + cost > MAX_SPEND_WEI) throw new Error("the spending limit for this run of the publisher is used up");
      spent += cost;
      return params;
    },
  });
  makeServer(env.PUBLISHER_TOKEN, (op) => publisher.apply(op), () => journal.unresolved().map((e) => e.opId))
    .listen(PORT, "127.0.0.1", () => console.log(`publisher ${account.address} listening on 127.0.0.1:${PORT}`));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error: unknown) => {
    const message = error instanceof Error ? error.message : "";
    console.error(/^(PUBLISHER_TOKEN|ARKIV_ACCESS_KEY|this key|wrong chain)/.test(message) ? message : `the publisher couldn't start (${error instanceof Error ? error.name : "unknown"})`);
    process.exit(1);
  });
}
