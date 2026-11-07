/** The real app in real Chrome at phone size, against a scripted API and a wallet with a real key. */

import { expect, test, type Page, type Route } from "@playwright/test";
import path from "node:path";
import { recoverTypedDataAddress } from "viem";
import { injectWallet } from "./support";

const KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // Foundry's public test key 0
const ID = `0x${"7a".repeat(32)}`;
const CAPTURE = process.env.E2E_SCREENSHOTS === "1";

const eip712Domain = [
  { name: "name", type: "string" }, { name: "version", type: "string" },
  { name: "chainId", type: "uint256" }, { name: "verifyingContract", type: "address" },
];
const permitType = [
  { name: "owner", type: "address" }, { name: "spender", type: "address" }, { name: "value", type: "uint256" },
  { name: "nonce", type: "uint256" }, { name: "deadline", type: "uint256" },
];
const intentType = [
  { name: "id", type: "bytes32" }, { name: "user", type: "address" }, { name: "amount", type: "uint256" },
  { name: "inrPaise", type: "uint64" }, { name: "rateE6", type: "uint64" }, { name: "qrCommitment", type: "bytes32" },
  { name: "deadline", type: "uint64" },
];
const disputeType = [{ name: "id", type: "bytes32" }, { name: "user", type: "address" }, { name: "deadline", type: "uint64" }];
const vault = "0x878734374e6131FCeEEB83FA8605Ab49f4D21836";
const vaultDomain = { name: "ScanPayVault", version: "1", chainId: 5042002, verifyingContract: vault };

const CONFIG = {
  paymaster: "0xEe5658e3479BB162B9a0D45F03e3ac0066A00BC1",
  spreadBps: 80,
  quoteTtlSec: 90,
  limits: { minInrPaise: "100", maxInrPaise: "100000", dailyUserInrPaise: "500000" },
  chains: {
    arc: {
      chainId: 5042002, name: "Arc Testnet", rpc: "https://rpc.testnet.arc.io", explorer: "https://explorer.testnet.arc.io", vault,
      token: { symbol: "USDC", address: "0x3600000000000000000000000000000000000000", decimals: 6 },
      vaultDomain, types: { EIP712Domain: eip712Domain, OfframpIntent: intentType, DisputeIntent: disputeType, Permit: permitType },
    },
    whitechain: {
      chainId: 1874, name: "Whitechain Sepolia", rpc: "https://rpc.testnet.whitechain.io", explorer: "https://explorer.testnet.whitechain.io", vault,
      token: { symbol: "USDT", address: "0xcedfB56113143d55b2358b8CFbB0dEB5Ad2f55a7", decimals: 6 },
      vaultDomain: { ...vaultDomain, chainId: 1874 }, types: { EIP712Domain: eip712Domain, OfframpIntent: intentType, DisputeIntent: disputeType, Permit: permitType },
    },
  },
};

interface Backend {
  quotes: Record<string, unknown>[];
  submits: Record<string, any>[]; // eslint-disable-line @typescript-eslint/no-explicit-any
  disputes: Record<string, any>[]; // eslint-disable-line @typescript-eslint/no-explicit-any
  status: string;
  payout: string;
  ttlSec: number;
  balance: string;
  viewTokensSeen: (string | null)[];
  submitError?: { status: number; error: string; message: string };
}

async function mockApi(page: Page, overrides: Partial<Backend> = {}): Promise<Backend> {
  const backend: Backend = {
    quotes: [], submits: [], disputes: [], status: "LOCKED", payout: "DISPATCHED", ttlSec: 90, balance: "50000000", viewTokensSeen: [], ...overrides,
  };
  const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const view = (viewToken?: string | null) => ({
    id: ID, chain: "arc", user: "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266", status: backend.status, payout: backend.payout,
    payee: viewToken === "view-token" ? { upiId: "shop@ybl", name: "Chai Point" } : null,
    amount: "253000", inrPaise: "2550", refundReason: null, refundReasonName: null,
    timers: { completableAt: "1", reclaimableAt: "2", releasableAt: String(Math.floor(Date.now() / 1000) + 86000), disputeDeadline: backend.status === "DISPUTED" ? String(Math.floor(Date.now() / 1000) + 600000) : null },
    canDispute: backend.status === "COMPLETED",
    txs: { lock: `0x${"11".repeat(32)}`, complete: backend.status === "LOCKED" ? null : `0x${"22".repeat(32)}`, dispute: null, final: null },
    explorer: null,
  });

  await page.route(/\/(config|account|quote|offramps?|dispute)(\/|\?|$)/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const p = url.pathname;
    if (request.resourceType() === "document") return route.fallback();
    if (p === "/config") return json(route, CONFIG);
    if (p.startsWith("/account/")) return json(route, { balance: backend.balance, balanceFormatted: String(Number(backend.balance) / 1e6), symbol: p.includes("arc") ? "USDC" : "USDT", permitNonce: "3" });
    if (p === "/quote") {
      const body = request.postDataJSON();
      backend.quotes.push(body);
      const chain = CONFIG.chains[body.chain as "arc" | "whitechain"];
      const deadline = Math.floor(Date.now() / 1000) + backend.ttlSec;
      const message = { id: ID, user: body.user, amount: "253000", inrPaise: "2550", rateE6: "100790000", qrCommitment: `0x${"ab".repeat(32)}`, deadline: String(deadline) };
      return json(route, {
        id: ID, intent: message, quoteSig: "0x00",
        payee: { upiId: "shop@ybl", name: "Chai Point", amountInQr: null },
        display: { amount: "0.253", symbol: chain.token.symbol, rate: "100.79", marketRate: "101.60", spreadBps: 80, networkFee: "₹0", expiresAt: String(deadline) },
        viewToken: "view-token",
        signing: {
          permit: {
            domain: { name: chain.token.symbol, version: "2", chainId: chain.chainId, verifyingContract: chain.token.address },
            types: { EIP712Domain: eip712Domain, Permit: permitType }, primaryType: "Permit",
            message: { owner: body.user, spender: vault, value: "253000", nonce: "3", deadline: String(deadline) },
          },
          intent: { domain: chain.vaultDomain, types: { EIP712Domain: eip712Domain, OfframpIntent: intentType }, primaryType: "OfframpIntent", message },
        },
      });
    }
    if (p === "/offramp") {
      if (backend.submitError) {
        const { status, error, message } = backend.submitError;
        backend.submitError = undefined;
        return json(route, { error, message }, status);
      }
      backend.submits.push(request.postDataJSON());
      return json(route, { id: ID, txHash: `0x${"11".repeat(32)}`, explorer: "https://x", paidBy: CONFIG.paymaster, status: "SUBMITTED" });
    }
    if (p === "/offramps") return json(route, { items: backend.submits.length ? [view(null)] : [] });
    if (p.startsWith("/offramp/")) {
      const token = request.headers()["x-view-token"] ?? null;
      backend.viewTokensSeen.push(token);
      return json(route, view(token));
    }
    if (p === "/dispute") {
      backend.disputes.push(request.postDataJSON());
      backend.status = "DISPUTED";
      return json(route, { id: ID, txHash: `0x${"33".repeat(32)}`, status: "DISPUTED" });
    }
    return route.fallback();
  });
  return backend;
}

async function shot(page: Page, name: string) {
  if (!CAPTURE) return;
  await page.waitForTimeout(400); // let MUI's colour transitions finish
  await page.screenshot({ path: `e2e/screenshots/${name}.png` });
}

async function connect(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Connect Test Wallet" }).click();
  await expect(page.getByRole("heading", { name: "Scan to pay" })).toBeVisible();
}

async function enterLink(page: Page, link = "upi://pay?pa=shop@ybl&pn=Chai%20Point") {
  await page.getByRole("button", { name: "Enter a UPI link" }).click();
  await page.getByLabel("UPI link").fill(link);
  await page.getByRole("button", { name: "Continue" }).click();
}

async function review(page: Page, amount = "25.50") {
  await enterLink(page);
  await page.getByLabel("Amount").fill(amount);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Review payment" })).toBeVisible();
}

test("without a wallet, a phone is offered MetaMask's browser", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  const link = page.getByRole("link", { name: "Open in MetaMask" });
  await expect(link).toHaveAttribute("href", /^https:\/\/metamask\.app\.link\/dapp\/127\.0\.0\.1:4173\//);
  await shot(page, "1-no-wallet");
});

test("full payment: both signatures recover to the wallet, then status and dispute", async ({ page }) => {
  const backend = await mockApi(page);
  const { account, state } = await injectWallet(page, { key: KEY });
  await connect(page);
  await shot(page, "2-home");
  await review(page);
  await shot(page, "3-review");
  await expect(page.getByText("0.253 USDC")).toBeVisible();
  await expect(page.getByRole("button", { name: /Pay · rate held/ })).toBeEnabled();
  await expect(page.getByText("Chai Point · shop@ybl", { exact: false })).toBeVisible();

  await page.getByRole("button", { name: /Pay · rate held/ }).click();
  await expect(page.getByRole("heading", { name: "Paying the merchant" })).toBeVisible();
  await shot(page, "4-paying");

  expect(state.chainId).toBe("0x4cef52"); // moved off mainnet before signing
  expect(backend.quotes[0]).toMatchObject({ chain: "arc", inr: "25.50", qrText: "upi://pay?pa=shop@ybl&pn=Chai%20Point" });

  const signs = state.calls.filter((c) => c.method === "eth_signTypedData_v4");
  expect(signs.map((c) => JSON.parse((c.params as string[])[1]).primaryType)).toEqual(["Permit", "OfframpIntent"]);

  const submit = backend.submits[0];
  const intentTyped = JSON.parse((signs[1].params as string[])[1]);
  const { EIP712Domain: _a, ...intentTypes } = intentTyped.types;
  expect(await recoverTypedDataAddress({ domain: intentTyped.domain, types: intentTypes, primaryType: "OfframpIntent", message: intentTyped.message, signature: submit.userSig })).toBe(account.address);
  expect(submit.intent.id).toBe(ID);

  // The permit is sent split, so rebuild it to prove r, s and v are right.
  const permitTyped = JSON.parse((signs[0].params as string[])[1]);
  const { EIP712Domain: _b, ...permitTypes } = permitTyped.types;
  expect([27, 28]).toContain(submit.permit.v);
  const rebuilt = `${submit.permit.r}${submit.permit.s.slice(2)}${submit.permit.v.toString(16)}` as `0x${string}`;
  expect(await recoverTypedDataAddress({ domain: permitTyped.domain, types: permitTypes, primaryType: "Permit", message: permitTyped.message, signature: rebuilt })).toBe(account.address);
  expect(submit.permit.deadline).toBe(String(permitTyped.message.deadline));

  expect(backend.viewTokensSeen).toContain("view-token");

  backend.status = "COMPLETED";
  await expect(page.getByRole("heading", { name: "Payment processed" })).toBeVisible({ timeout: 8000 });
  await expect(page.getByText("merchant has", { exact: false })).toHaveCount(0);
  await shot(page, "5-processed");

  await page.getByRole("button", { name: /Dispute/ }).click();
  await shot(page, "6-dispute-dialog");
  await page.getByRole("dialog").getByRole("button", { name: "Dispute" }).click();
  await expect(page.getByRole("heading", { name: "Under review" })).toBeVisible({ timeout: 8000 });
  await shot(page, "7-disputed");

  const disputeCall = state.calls.filter((c) => c.method === "eth_signTypedData_v4").at(-1)!;
  const disputeTyped = JSON.parse((disputeCall.params as string[])[1]);
  const { EIP712Domain: _c, ...disputeTypes } = disputeTyped.types;
  const sent = backend.disputes[0];
  expect(disputeTyped.primaryType).toBe("DisputeIntent");
  expect(Object.keys(disputeTyped.types).sort()).toEqual(["DisputeIntent", "EIP712Domain"]);
  expect(sent.deadline).toBe(disputeTyped.message.deadline);
  expect(await recoverTypedDataAddress({ domain: disputeTyped.domain, types: disputeTypes, primaryType: "DisputeIntent", message: disputeTyped.message, signature: sent.userSig })).toBe(account.address);
});

test("a chain the wallet doesn't know gets added with the right gas coin", async ({ page }) => {
  const backend = await mockApi(page);
  const { state } = await injectWallet(page, { key: KEY, knownChains: ["0x1"] });
  await connect(page);
  await review(page);
  await page.getByRole("button", { name: /Pay · rate held/ }).click();
  await expect(page.getByRole("heading", { name: "Paying the merchant" })).toBeVisible();
  const add = state.calls.find((c) => c.method === "wallet_addEthereumChain")!;
  expect((add.params as object[])[0]).toMatchObject({ chainId: "0x4cef52", nativeCurrency: { symbol: "USDC", decimals: 18 }, rpcUrls: ["https://rpc.testnet.arc.io"] });
  expect(backend.submits).toHaveLength(1);
});

test("the second token works: Whitechain signs on its own chain", async ({ page }) => {
  const backend = await mockApi(page);
  const { state } = await injectWallet(page, { key: KEY });
  await connect(page);
  await enterLink(page);
  await page.getByLabel("Amount").fill("10");
  await page.getByRole("button", { name: /USDT/ }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByRole("button", { name: /Pay · rate held/ }).click();
  await expect(page.getByRole("heading", { name: "Paying the merchant" })).toBeVisible();
  expect(backend.quotes[0].chain).toBe("whitechain");
  expect(state.chainId).toBe("0x752");
});

test("declining in the wallet explains it, sends nothing, and can be retried", async ({ page }) => {
  const backend = await mockApi(page);
  await injectWallet(page, { key: KEY, rejectMethods: ["eth_signTypedData_v4"] });
  await connect(page);
  await review(page);
  await page.getByRole("button", { name: /Pay · rate held/ }).click();
  await expect(page.getByRole("alert")).toContainText("cancelled");
  expect(backend.submits).toHaveLength(0);
  await page.getByRole("button", { name: /Pay · rate held/ }).click();
  await expect(page.getByRole("heading", { name: "Paying the merchant" })).toBeVisible();
  expect(backend.submits).toHaveLength(1);
});

test("declining the connection shows why", async ({ page }) => {
  await mockApi(page);
  await injectWallet(page, { key: KEY, rejectMethods: ["eth_requestAccounts"] });
  await page.goto("/");
  await page.getByRole("button", { name: "Connect Test Wallet" }).click();
  await expect(page.getByRole("alert")).toContainText("cancelled");
});

test("a backend refusal shows its message and stays on the review", async ({ page }) => {
  await mockApi(page, { submitError: { status: 429, error: "DAILY_LIMIT", message: "raw" } });
  await injectWallet(page, { key: KEY });
  await connect(page);
  await review(page);
  await page.getByRole("button", { name: /Pay · rate held/ }).click();
  await expect(page.getByRole("alert")).toContainText("today's payment limit");
  await expect(page.getByRole("heading", { name: "Review payment" })).toBeVisible();
});

test("an expired rate can't be signed, only refreshed", async ({ page }) => {
  const backend = await mockApi(page, { ttlSec: 2 });
  await injectWallet(page, { key: KEY });
  await connect(page);
  await review(page);
  backend.ttlSec = 90;
  await expect(page.getByRole("button", { name: "Get a new rate" })).toBeVisible({ timeout: 6000 });
  await expect(page.getByRole("button", { name: /Pay · rate held/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Get a new rate" }).click();
  await expect(page.getByRole("button", { name: /Pay · rate held/ })).toBeVisible();
  expect(backend.quotes).toHaveLength(2);
});

/** Answers the Arc RPC so the swap card's EURC balance read never leaves the test. */
async function mockArcRpc(page: Page, eurcUnits: bigint) {
  const calls: string[] = [];
  await page.route("https://rpc.testnet.arc.io/**", async (route) => {
    const body = route.request().postDataJSON();
    const answer = (req: { id: number; method: string }) => {
      calls.push(req.method);
      const result = req.method === "eth_call" ? `0x${eurcUnits.toString(16).padStart(64, "0")}`
        : req.method === "eth_chainId" ? "0x4cef52" : "0x0";
      return { jsonrpc: "2.0", id: req.id, result };
    };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(Array.isArray(body) ? body.map(answer) : answer(body)) });
  });
  return calls;
}

test("too little USDC on Arc offers a swap from EURC and keeps Pay blocked", async ({ page }) => {
  await mockApi(page, { balance: "100" });
  const rpcCalls = await mockArcRpc(page, 2_500_000n);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await review(page);
  await expect(page.getByText("This payment needs")).toBeVisible();
  await expect(page.getByText("Your EURC: 2.5")).toBeVisible();
  await expect(page.getByRole("button", { name: "Swap EURC → USDC" })).toBeEnabled();
  // The swap card replaces the generic warning on Arc.
  await expect(page.getByText("doesn't have enough USDC")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Pay · rate held/ })).toBeDisabled();
  expect(rpcCalls).toContain("eth_call");
  await shot(page, "swap-1-card");
  if (!CAPTURE) return;
  // Screenshot mode only: open the confirm dialog with a LIVE Circle Swap Kit estimate (read-only, nothing is sent).
  await page.getByRole("button", { name: "Swap EURC → USDC" }).click();
  const dialog = page.getByRole("dialog", { name: "Confirm the swap" });
  await expect(dialog).toBeVisible({ timeout: 60_000 });
  await expect(dialog.getByText(/→ at least/)).toBeVisible();
  await shot(page, "swap-2-confirm");
});

test("too little USDC on Arc with no EURC disables the swap", async ({ page }) => {
  await mockApi(page, { balance: "100" });
  await mockArcRpc(page, 0n);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await review(page);
  await expect(page.getByText("You don't have any EURC to swap.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Swap EURC → USDC" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /Pay · rate held/ })).toBeDisabled();
});

test("too little USDT on Whitechain blocks paying, with no swap offered", async ({ page }) => {
  await mockApi(page, { balance: "100" });
  await injectWallet(page, { key: KEY });
  await connect(page);
  await enterLink(page);
  await page.getByLabel("Amount").fill("25.50");
  await page.getByRole("button", { name: /USDT · Whitechain/ }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Review payment" })).toBeVisible();
  await expect(page.getByText("doesn't have enough USDT")).toBeVisible();
  await expect(page.getByRole("button", { name: "Swap EURC → USDC" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Pay · rate held/ })).toBeDisabled();
});

test("amount limits and bad QR text are caught before any quote", async ({ page }) => {
  const backend = await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await enterLink(page, "https://not-upi.example");
  await expect(page.getByRole("alert")).toContainText("isn't a UPI");
  await page.getByLabel("UPI link").fill("upi://pay?pa=shop@ybl");
  await page.getByRole("button", { name: "Continue" }).click();
  const amount = page.getByLabel("Amount");
  const go = page.getByRole("button", { name: "Continue" });
  await expect(go).toBeDisabled();
  for (const bad of ["0.5", "1000.01", "abc", "1.234"]) {
    await amount.fill(bad);
    await expect(go).toBeDisabled();
  }
  await amount.fill("1000");
  await expect(go).toBeEnabled();
  expect(backend.quotes).toHaveLength(0);
});

test("a QR with its own amount can't be changed", async ({ page }) => {
  await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await enterLink(page, "upi://pay?pa=shop@ybl&am=25.50");
  await expect(page.getByLabel("Amount")).toBeDisabled();
  await expect(page.getByLabel("Amount")).toHaveValue("25.50");
  await expect(page.getByText("Set by this QR code")).toBeVisible();
});

test("uploading a photo reads the QR", async ({ page }) => {
  await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await page.getByLabel("QR code image").setInputFiles(path.join(import.meta.dirname, "fixtures/upi-qr.png"));
  await expect(page.getByRole("heading", { name: "Fixture Shop" })).toBeVisible();
  await expect(page.getByLabel("Amount")).toHaveValue("25.50");
});

test("an image with no QR says so", async ({ page }) => {
  await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await page.getByLabel("QR code image").setInputFiles(path.join(import.meta.dirname, "../public/pwa-192x192.png"));
  await expect(page.getByRole("alert")).toContainText("No QR code found");
});

test("history shows this device's payee names, and opens a payment", async ({ page }) => {
  const backend = await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await page.getByRole("button", { name: "History" }).click();
  await expect(page.getByText("Your payments will show up here.")).toBeVisible();
  await page.getByRole("button", { name: "Pay", exact: true }).click();
  await review(page);
  await page.getByRole("button", { name: /Pay · rate held/ }).click();
  await expect(page.getByRole("heading", { name: "Paying the merchant" })).toBeVisible();
  backend.status = "COMPLETED";
  await page.getByRole("button", { name: "History" }).click();
  await expect(page.getByText("Chai Point")).toBeVisible();
  await shot(page, "8-history");
  await page.getByText("Chai Point").click();
  await expect(page.getByRole("heading", { name: "Payment processed" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Dispute/ })).toBeVisible();
  await page.getByRole("button", { name: "Back to history" }).click();
});

test("reloading reconnects silently, and Disconnect forgets it", async ({ page }) => {
  await mockApi(page);
  const { state } = await injectWallet(page, { key: KEY });
  await connect(page);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Scan to pay" })).toBeVisible();
  expect(state.calls.filter((c) => c.method === "eth_requestAccounts")).toHaveLength(1);
  await page.getByRole("button", { name: "Wallet menu" }).click();
  await page.getByRole("menuitem", { name: "Disconnect" }).click();
  await expect(page.getByRole("button", { name: "Connect Test Wallet" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "Connect Test Wallet" })).toBeVisible();
});

test("the backend being down offers a retry", async ({ page }) => {
  await page.route(/\/config$/, (route) => route.abort());
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Can't reach RupeeRail");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("compliance FAQ opens from the top bar, expands an answer, and goes back", async ({ page }) => {
  await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await page.getByRole("link", { name: "Compliance and FAQ" }).click();
  await expect(page).toHaveURL(/\/compliance$/);
  await expect(page.getByRole("heading", { name: "Compliance and FAQ" })).toBeVisible();
  await expect(page.getByText("each wallet can pay up to ₹5,000 a day")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Beyond the hackathon" })).toBeVisible();

  const question = page.getByRole("button", { name: /What regulatory registrations are required/ });
  await expect(question).toHaveAttribute("aria-expanded", "false");
  await question.click();
  await expect(question).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText(/RupeeRail is not yet registered with FIU-IND/)).toBeVisible();

  const next = page.getByRole("button", { name: /Wire Arc mainnet and keep production keys separate/ });
  await next.click();
  await expect(page.getByText(/0xa140978374AAE1637b927C568E584e8fBB9a7cb0/)).toBeVisible();
  await shot(page, "10-compliance");

  await page.goBack();
  await expect(page.getByRole("heading", { name: "Scan to pay" })).toBeVisible();
  await page.goForward();
  await page.getByRole("button", { name: "History" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Compliance and FAQ" })).toHaveCount(0);
});

test("/compliance loads directly, even with the backend down", async ({ page }) => {
  await page.route(/\/config$/, (route) => route.abort());
  await page.goto("/compliance");
  await expect(page.getByRole("heading", { name: "Compliance and FAQ" })).toBeVisible();
  await page.getByRole("button", { name: /Is operating a USDC-to-INR off-ramp/ }).click();
  await expect(page.getByText(/IAMAI\) v\. Reserve Bank of India/)).toBeVisible();
  await page.getByRole("button", { name: "Back" }).click();
  await expect(page.getByRole("alert")).toContainText("Can't reach RupeeRail");
});

for (const width of [320, 360, 412, 768, 1280]) {
  test(`nothing overflows sideways at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 700 });
    await mockApi(page);
    await injectWallet(page, { key: KEY });
    await connect(page);
    const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(await fits()).toBe(true);
    await enterLink(page, "upi://pay?pa=a-very-long-merchant-name-for-testing-overflow@okhdfcbank&pn=A%20Really%20Long%20Merchant%20Business%20Name%20Private%20Limited");
    expect(await fits()).toBe(true);
    await page.getByLabel("Amount").fill("25.50");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByRole("heading", { name: "Review payment" })).toBeVisible();
    expect(await fits()).toBe(true);
  });
}

test("dark mode follows the device and stays readable", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  await review(page);
  await shot(page, "9-dark-review");
  const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(background).toBe("rgb(16, 20, 24)");
});

test("every button is big enough to tap", async ({ page }) => {
  await mockApi(page);
  await injectWallet(page, { key: KEY });
  await connect(page);
  const small = await page.locator("main button:visible").evaluateAll((elements) =>
    elements.filter((e) => e.getBoundingClientRect().height < 44).map((e) => e.textContent));
  expect(small).toEqual([]);
});

test("PWA: installable manifest, and the app shell loads offline", async ({ page, context }) => {
  await mockApi(page);
  await page.goto("/");
  const manifest = await (await page.request.get("/manifest.webmanifest")).json();
  expect(manifest).toMatchObject({ name: "RupeeRail", display: "standalone", start_url: "/" });
  for (const size of ["192x192", "512x512"]) expect(manifest.icons.some((i: { sizes: string }) => i.sizes === size)).toBe(true);
  expect(manifest.icons.some((i: { purpose?: string }) => i.purpose === "maskable")).toBe(true);
  for (const icon of manifest.icons) expect((await page.request.get(icon.src)).ok()).toBe(true);

  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload(); // now controlled by the service worker
  await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByRole("heading", { name: "RupeeRail" })).toBeVisible();
  // The shell works offline, but live data must not come from a cache.
  await expect(page.locator("#root")).not.toBeEmpty();
  await context.setOffline(false);
});

test("PWA: the service worker never serves API responses from its cache", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(() => navigator.serviceWorker.ready);
  const cached = await page.evaluate(async () => {
    const names = await caches.keys();
    const urls: string[] = [];
    for (const name of names) for (const request of await (await caches.open(name)).keys()) urls.push(new URL(request.url).pathname);
    return urls;
  });
  expect(cached.length).toBeGreaterThan(0);
  expect(cached.filter((u) => /^\/(config|quote|offramp|account|dispute|events|health)/.test(u))).toEqual([]);
});
