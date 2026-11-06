/**
 * Buy (INR -> tokens) in real Chrome at phone size: a scripted API, and a fake Razorpay page that sends the
 * browser back to /?onramp=<id>&payment=received exactly like the backend's callback redirect does.
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { injectWallet } from "./support";

const KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // Foundry's public test key 0
const WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
const ORDER = `0x${"0f".repeat(32)}`;
const RAZORPAY = "https://rzp.test/rzp/abc";
const CAPTURE = process.env.E2E_SCREENSHOTS === "1";

const chain = (name: string, symbol: string, chainId: number) => ({
  chainId, name, rpc: "https://rpc.invalid", explorer: "https://explorer.testnet.arc.io",
  vault: "0x878734374e6131FCeEEB83FA8605Ab49f4D21836",
  token: { symbol, address: "0x3600000000000000000000000000000000000000", decimals: 6 },
  vaultDomain: {}, types: {},
});
const CONFIG = {
  spreadBps: 80, quoteTtlSec: 90,
  limits: { minInrPaise: "100", maxInrPaise: "100000", dailyUserInrPaise: "500000" },
  chains: { arc: chain("Arc Testnet", "USDC", 5042002), whitechain: chain("Whitechain Sepolia", "USDT", 1874) },
};

interface Backend {
  created: Record<string, string>[];
  statuses: string[]; // returned in order by GET /onramp/{id}; the last one repeats
  createError?: { status: number; error: string; message: string };
}

async function mockApi(page: Page, overrides: Partial<Backend> = {}): Promise<Backend> {
  const backend: Backend = { created: [], statuses: ["AWAITING_FIAT", "PAID"], ...overrides };
  const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const order = (status: string) => ({
    orderId: ORDER, chain: "arc", wallet: WALLET, inrPaise: "1000", amount: "0.099062", amountBaseUnits: "99062",
    symbol: "USDC", rate: "100.95", payTo: null,
    checkout: { provider: "razorpay", url: RAZORPAY, linkId: "plink_T1", expiresAt: String(Math.floor(Date.now() / 1000) + 960),
                paymentId: status === "AWAITING_FIAT" ? null : "pay_T1" },
    status, paidTx: status === "PAID" ? `0x${"87".repeat(32)}` : null,
    explorer: status === "PAID" ? `https://explorer.testnet.arc.io/tx/0x${"87".repeat(32)}` : null, lastError: null,
  });

  await page.route(/\/(config|onramp)(\/|\?|$)/, async (route) => {
    const request = route.request();
    if (request.resourceType() === "document") return route.fallback();
    const p = new URL(request.url()).pathname;
    if (p === "/config") return json(route, CONFIG);
    if (p === "/onramp" && request.method() === "POST") {
      if (backend.createError) {
        const { status, error, message } = backend.createError;
        backend.createError = undefined;
        return json(route, { error, message }, status);
      }
      backend.created.push(request.postDataJSON());
      return json(route, order("AWAITING_FIAT"));
    }
    if (p === `/onramp/${ORDER}`) {
      const status = backend.statuses.length > 1 ? backend.statuses.shift()! : backend.statuses[0];
      return json(route, order(status));
    }
    return route.fallback();
  });
  // Razorpay's hosted page: "pay", then redirect back the way the backend's callback does.
  await page.route(`${RAZORPAY}**`, (route) =>
    route.fulfill({ status: 303, headers: { location: `http://127.0.0.1:4173/?onramp=${ORDER}&payment=received` } }));
  return backend;
}

async function shot(page: Page, name: string) {
  if (!CAPTURE) return;
  await page.waitForTimeout(400);
  await page.screenshot({ path: `e2e/screenshots/${name}.png` });
}

async function openBuy(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Connect Test Wallet" }).click();
  await page.getByRole("button", { name: "Buy" }).click();
  await expect(page.getByRole("heading", { name: "Buy stablecoins" })).toBeVisible();
}

test("buy with Razorpay, come back and see the tokens arrive", async ({ page }) => {
  await injectWallet(page, { key: KEY });
  const backend = await mockApi(page, { statuses: ["AWAITING_FIAT", "PAYING", "PAID"] });
  await openBuy(page);
  await shot(page, "buy-1-form");

  await page.getByLabel("Amount").fill("10");
  await page.getByRole("button", { name: "Continue with ₹10" }).click();
  await expect(page.getByRole("heading", { name: "Review purchase" })).toBeVisible();
  await expect(page.getByText("0.099062 USDC").first()).toBeVisible();
  expect(backend.created).toEqual([{ chain: "arc", wallet: WALLET.toLowerCase(), inr: "10" }]);
  await shot(page, "buy-2-review");

  await page.getByRole("button", { name: "Pay ₹10 with Razorpay" }).click();
  await page.waitForURL("http://127.0.0.1:4173/"); // came back, and the return params were removed
  await expect(page.getByRole("heading", { name: "0.099062 USDC sent" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("link", { name: /View transaction/ })).toHaveAttribute("href", /\/tx\/0x8787/);
  await expect(page.getByText("pay_T1")).toBeVisible();
  await shot(page, "buy-3-sent");

  await page.getByRole("button", { name: "Buy more" }).click();
  await expect(page.getByText("Recent purchases")).toBeVisible();
  await expect(page.getByText("0.099062 USDC for ₹10")).toBeVisible();
});

test("returning from Razorpay before reconnecting still shows the purchase", async ({ page }) => {
  await mockApi(page, { statuses: ["PAID"] });
  await page.goto(`/?onramp=${ORDER}&payment=received`);
  await expect(page.getByRole("heading", { name: "0.099062 USDC sent" })).toBeVisible();
  await expect(page).toHaveURL("http://127.0.0.1:4173/");
});

test("a pool that can't cover the amount is explained, and nothing is charged", async ({ page }) => {
  await injectWallet(page, { key: KEY });
  const backend = await mockApi(page, { createError: { status: 409, error: "POOL_INSUFFICIENT", message: "raw" } });
  await openBuy(page);
  await page.getByLabel("Amount").fill("500");
  await page.getByRole("button", { name: "Continue with ₹500" }).click();
  await expect(page.getByRole("alert")).toHaveText("We can't sell that much right now. Try a smaller amount.");
  expect(backend.created).toEqual([]);
});

test("amounts outside the limits are refused before asking the server", async ({ page }) => {
  await injectWallet(page, { key: KEY });
  const backend = await mockApi(page);
  await openBuy(page);
  await page.getByLabel("Amount").fill("5000");
  await expect(page.getByText("Enter between ₹1 and ₹1,000")).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue" })).toBeDisabled();
  expect(backend.created).toEqual([]);
});
