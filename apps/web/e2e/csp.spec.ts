/** The swap under the CSP the server really sends: a browser-side host missing from connect-src only fails in production. */

import { expect, test, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { injectWallet } from "./support";

const KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // Foundry's public test key
const CSP = /Content-Security-Policy "([^"]+)"/.exec(readFileSync(path.join(import.meta.dirname, "../deploy/Caddyfile"), "utf8"))![1];
const ID = `0x${"7a".repeat(32)}`;
const RPC = "https://rpc.testnet.arc.io";
const CONFIG = {
  spreadBps: 80, quoteTtlSec: 90,
  limits: { minInrPaise: "100", maxInrPaise: "100000", dailyUserInrPaise: "500000" },
  chains: {
    arc: {
      chainId: 5042002, name: "Arc Testnet", rpc: RPC, explorer: "https://explorer.testnet.arc.io",
      vault: "0x878734374e6131FCeEEB83FA8605Ab49f4D21836", vaultDomain: {}, types: {},
      token: { symbol: "USDC", address: "0x3600000000000000000000000000000000000000", decimals: 6 },
    },
  },
};
const QUOTE = (user: string) => ({
  id: ID, quoteSig: "0x00", viewToken: "t", signing: {},
  intent: { id: ID, user, amount: "2000000", inrPaise: "20000", rateE6: "100000000", qrCommitment: ID, deadline: "9999999999" },
  payee: { upiId: "shop@ybl", name: "Shop", amountInQr: null },
  display: { amount: "2", symbol: "USDC", rate: "100", marketRate: "101", spreadBps: 80, networkFee: "₹0", expiresAt: String(Math.floor(Date.now() / 1000) + 90) },
});

test("the swap can reach the Arc RPC and Circle under the production CSP", async ({ page }) => {
  await injectWallet(page, { key: KEY });
  await page.addInitScript(() => {
    (window as unknown as { __blocked: string[] }).__blocked = [];
    document.addEventListener("securitypolicyviolation", (e) => (window as unknown as { __blocked: string[] }).__blocked.push(e.blockedURI));
  });
  const json = (route: Route, body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });

  // Requests stubbed here still have to pass the CSP first, because the browser checks it before sending anything.
  const reached = new Set<string>();
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.host !== new URL(page.url() === "about:blank" ? request.url() : page.url()).host) reached.add(url.origin);
    if (url.origin === RPC) {
      const body = request.postDataJSON();
      const answer = (r: { id: number; method: string }) => ({
        jsonrpc: "2.0", id: r.id,
        result: r.method === "eth_call" ? `0x${(5_000_000n).toString(16).padStart(64, "0")}` : r.method === "eth_chainId" ? "0x4cef52" : "0x0",
      });
      return json(route, Array.isArray(body) ? body.map(answer) : answer(body));
    }
    if (url.origin === "https://api.circle.com") return route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
    if (url.pathname === "/config") return json(route, CONFIG);
    if (url.pathname.startsWith("/account/")) return json(route, { balance: "100", balanceFormatted: "0.0001", symbol: "USDC", permitNonce: "0" });
    if (url.pathname === "/quote") return json(route, QUOTE(request.postDataJSON().user));
    if (request.resourceType() === "document") {
      const response = await route.fetch();
      return route.fulfill({ response, headers: { ...response.headers(), "content-security-policy": CSP } });
    }
    return route.continue();
  });

  await page.goto("/");
  await page.getByRole("button", { name: /^Connect/ }).click();
  await page.getByRole("button", { name: "Enter a UPI link" }).click();
  await page.getByLabel("UPI link").fill("upi://pay?pa=shop@ybl&pn=Shop");
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByLabel("Amount").fill("200");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Your EURC: 5")).toBeVisible();

  await page.getByRole("button", { name: "Swap EURC → USDC" }).click();
  await expect(page.getByRole("alert")).toBeVisible({ timeout: 30_000 }); // Circle is stubbed to fail, after being reached
  expect([...reached]).toEqual(expect.arrayContaining([RPC, "https://api.circle.com"]));
  expect(await page.evaluate(() => (window as unknown as { __blocked: string[] }).__blocked)).toEqual([]);
});
