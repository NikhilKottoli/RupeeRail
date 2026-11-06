/**
 * Real wallet signatures through the real app against a deployed backend. IT SENDS TESTNET TRANSACTIONS.
 * Skipped unless E2E_BASE_URL, E2E_WALLET_KEY and E2E_UPI_LINK are set. Keep apps/upi-payer stopped or in
 * dry-run, or a phone may pay the UPI link real rupees.
 */

import { expect, test } from "@playwright/test";
import { injectWallet } from "./support";

const { E2E_BASE_URL: url, E2E_WALLET_KEY: rawKey, E2E_UPI_LINK: link, E2E_DISPUTE: dispute } = process.env;

test.describe("live", () => {
  test.skip(!url || !rawKey || !link, "set E2E_BASE_URL, E2E_WALLET_KEY and E2E_UPI_LINK");
  test.setTimeout(240_000);

  test("pays ₹1 through the deployed backend", async ({ page }) => {
    const key = (rawKey!.startsWith("0x") ? rawKey! : `0x${rawKey}`) as `0x${string}`;
    const { state } = await injectWallet(page, { key, knownChains: ["0x1"] });
    await page.goto("/");
    await page.getByRole("button", { name: "Connect Test Wallet" }).click();
    await page.getByRole("button", { name: "Enter a UPI link" }).click();
    await page.getByLabel("UPI link").fill(link!);
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByLabel("Amount").fill("1");
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: /Pay · rate held/ }).click();
    await expect(page.getByRole("heading", { name: "Paying the merchant" })).toBeVisible({ timeout: 60_000 });
    await expect(page.getByRole("heading", { name: "Payment processed" })).toBeVisible({ timeout: 120_000 });
    const hrefs = await page.getByRole("link", { name: /Lock|Completion/ }).evaluateAll((links) => links.map((a) => (a as HTMLAnchorElement).href));
    console.log("explorer links:", hrefs.join(" "));
    expect(state.calls.some((c) => c.method === "wallet_addEthereumChain")).toBe(true);

    if (dispute === "1") {
      await page.getByRole("button", { name: /Dispute/ }).click();
      await page.getByRole("dialog").getByRole("button", { name: "Dispute" }).click();
      await expect(page.getByRole("heading", { name: "Under review" })).toBeVisible({ timeout: 120_000 });
      console.log("dispute relayed and shown as DISPUTED");
    }

    await page.getByRole("button", { name: "History" }).click();
    await expect(page.getByText(/1 Nov|\d+ \w+ \d{4}/).first()).toBeVisible({ timeout: 20_000 });
  });
});
