/**
 * Arkiv clients and write safety, following Arkiv's first-write and write-safety guidance for SDK 1.0.x on Rabdi:
 * an unsigned estimate, then explicit nonce, gas and gas price, a journal entry before signing, a receipt check,
 * and no automatic retries. Errors are reduced to fixed messages, because the RPC URL contains the access key.
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createPublicClient, createWalletClient, type TxParams } from "@arkiv-network/sdk";
import { rabdi } from "@arkiv-network/sdk/chains";
import { custom, http, MethodNotFoundRpcError, webSocket, type LocalAccount } from "viem";
import { getGasPrice } from "viem/actions";

export const RABDI_CHAIN_ID = 7794095;
export const SERVICE_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const SINGLE_OP_GAS_FLOOR = 1_000_000n;
const EXTRA_OP_GAS = 400_000n;

/** The process's environment, over the service's .env if there is one. The deployed reader has no .env. */
export function loadEnv(path = join(SERVICE_DIR, ".env")): Record<string, string> {
  const env: Record<string, string> = {};
  const text = existsSync(path) ? readFileSync(path, "utf8") : "";
  for (const [key, value] of Object.entries(process.env)) if (value) env[key] = value;
  for (const line of text.split("\n")) {
    const index = line.indexOf("=");
    if (index > 0 && !line.trimStart().startsWith("#")) {
      const key = line.slice(0, index).trim();
      if (!process.env[key]) env[key] = line.slice(index + 1).trim().replace(/^["']|["']$/g, "");
    }
  }
  return env;
}

export function makeClients(account: LocalAccount, accessKey: string) {
  if (!accessKey) throw new Error("ARKIV_ACCESS_KEY is not set");
  const chain = rabdi({ accessKey });
  const transport = http(undefined, { retryCount: 0, timeout: 15_000, fetchOptions: { cache: "no-store", redirect: "error" } });
  return {
    reader: createPublicClient({ chain, transport, cacheTime: 0 }),
    wallet: createWalletClient({ account, chain, transport, cacheTime: 0 }),
  };
}

/** A client with no signer, for the public reader. */
export function makeReader(accessKey: string) {
  if (!accessKey) throw new Error("ARKIV_ACCESS_KEY is not set");
  return createPublicClient({
    chain: rabdi({ accessKey }),
    transport: http(undefined, { retryCount: 0, timeout: 15_000, fetchOptions: { cache: "no-store", redirect: "error" } }),
    cacheTime: 0,
  });
}

/** Real pushes rather than polling, because watchEntityEvents opens a socket only over a WebSocket transport. */
export function makeWatcher(accessKey: string) {
  if (!accessKey) throw new Error("ARKIV_ACCESS_KEY is not set");
  return createPublicClient({ chain: rabdi({ accessKey }), transport: webSocket(undefined, { retryCount: 0 }), cacheTime: 0 });
}

export type Clients = ReturnType<typeof makeClients>;
type Wallet = ReturnType<typeof createWalletClient>;

/** Runs the exact write against a wallet that can't sign, and returns the raw gas estimate. */
export async function estimateWrite(account: LocalAccount, accessKey: string, prepare: (wallet: Wallet) => Promise<unknown>): Promise<bigint> {
  const chain = rabdi({ accessKey });
  const rpc = http(undefined, { retryCount: 0, timeout: 15_000, fetchOptions: { cache: "no-store", redirect: "error" } })({ chain });
  const allowed = new Set(["eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getTransactionCount", "eth_gasPrice", "eth_maxPriorityFeePerGas", "eth_estimateGas"]);
  let estimate: bigint | undefined;
  const captured = new Error("estimate captured");
  const transport = custom({
    async request(args: { method: string; params?: unknown[] }) {
      if (args.method === "eth_fillTransaction") throw new MethodNotFoundRpcError(new Error("use the guarded estimate"));
      if (!allowed.has(args.method)) throw new Error("mutating RPC blocked");
      if (args.method === "eth_estimateGas") {
        const result = await rpc.request({ method: "eth_estimateGas", params: [args.params?.[0], "latest"] } as never);
        estimate = BigInt(result as string);
        throw captured;
      }
      return rpc.request(args as never);
    },
  }, { retryCount: 0 });
  const unsigned = { ...account, signTransaction: async () => { throw new Error("signing blocked"); } } as LocalAccount;
  try {
    await prepare(createWalletClient({ account: unsigned, chain, transport }));
  } catch (error) {
    let cause: unknown = error;
    for (let depth = 0; cause && depth < 24; depth++) {
      if (cause === captured && estimate && estimate > 0n) return estimate;
      cause = typeof cause === "object" ? (cause as { cause?: unknown }).cause : undefined;
    }
    throw new Error("unsigned estimate failed, so nothing was signed");
  }
  throw new Error("the estimate never ran");
}

/** Explicit parameters for one operation, refusing if the cost is over budget or a transaction is pending. */
export async function txParamsFor(reader: Clients["reader"], address: `0x${string}`, estimate: bigint, maxCostWei: bigint, operations = 1): Promise<TxParams> {
  const doubled = 2n * estimate;
  const floor = SINGLE_OP_GAS_FLOOR + EXTRA_OP_GAS * BigInt(operations - 1);
  const gas = doubled > floor ? doubled : floor;
  const block = await reader.getBlock({ blockTag: "latest" });
  if (gas > block.gasLimit / 2n) throw new Error("gas is over half the block limit");
  const nonce = await reader.getTransactionCount({ address, blockTag: "latest" });
  if ((await reader.getTransactionCount({ address, blockTag: "pending" })) !== nonce) throw new Error("this wallet has a pending transaction; reconcile it first");
  const gasPrice = await getGasPrice(reader);
  const cost = gas * gasPrice;
  if (cost > maxCostWei) throw new Error(`gas cost ${cost} wei is over the ${maxCostWei} wei budget`);
  if (cost > (await reader.getBalance({ address }))) throw new Error("the wallet can't afford the gas");
  return { nonce, gas, gasPrice };
}
