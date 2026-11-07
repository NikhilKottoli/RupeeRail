/** The shape of the app: a sidebar on wide screens, a bottom bar on phones, and the shared Material 3 Expressive list. */

import { expect, test, type Page } from "@playwright/test";
import { injectWallet } from "./support";

const KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // Foundry's public test key
const USER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
const CAPTURE = process.env.E2E_SCREENSHOTS === "1";

function payment(id: string, inrPaise: string) {
  return {
    id, chain: "arc", user: USER, status: "COMPLETED", payout: "DISPATCHED", payee: null, amount: "253000", inrPaise,
    refundReason: null, refundReasonName: null, canDispute: false, explorer: null,
    timers: { completableAt: "1", reclaimableAt: "2", releasableAt: "3", disputeDeadline: null },
    txs: { lock: null, complete: null, dispute: null, final: null },
  };
}

async function open(page: Page) {
  await injectWallet(page, { key: KEY });
  await page.route("**/config", (route) => route.fulfill({ json: { chains: {}, limits: { minInrPaise: "100", maxInrPaise: "100000" } } }));
  await page.route("**/offramps?*", (route) => route.fulfill({
    json: { items: [payment(`0x${"1".repeat(64)}`, "2550"), payment(`0x${"2".repeat(64)}`, "12000"), payment(`0x${"3".repeat(64)}`, "99900")] },
  }));
  await page.goto("/");
  await page.getByRole("button", { name: /^Connect/ }).click();
  await expect(page.getByRole("heading", { name: "Scan to pay" })).toBeVisible();
}

async function shot(page: Page, name: string) {
  if (!CAPTURE) return;
  await page.waitForTimeout(400);
  await page.screenshot({ path: `e2e/screenshots/${name}.png` });
}

test.describe("on a desktop", () => {
  test.use({ viewport: { width: 1280, height: 800 }, isMobile: false, hasTouch: false });

  test("navigation is a sidebar, with each icon and label on one line", async ({ page }) => {
    await open(page);
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav).toBeVisible();
    await expect(page.locator(".MuiBottomNavigation-root")).toHaveCount(0);
    expect((await nav.boundingBox())!.x).toBe(0);

    const pay = nav.getByRole("button", { name: "Pay", exact: true });
    await expect(pay).toHaveAttribute("aria-current", "page");
    const icon = (await pay.locator("svg").boundingBox())!;
    const label = (await pay.locator("span").boundingBox())!;
    expect(Math.abs((icon.y + icon.height / 2) - (label.y + label.height / 2))).toBeLessThan(2);
    expect(label.x).toBeGreaterThan(icon.x + icon.width);
    await shot(page, "layout-desktop-pay");

    await nav.getByRole("button", { name: "History", exact: true }).click();
    await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
    await expect(nav.getByRole("button", { name: "History", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(pay).not.toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: "RupeeRail" })).toHaveCount(1);
    await shot(page, "layout-desktop-history");
  });

  test("the main actions sit inline, and don't stretch across the page", async ({ page }) => {
    await open(page);
    const scan = (await page.getByRole("button", { name: "Scan QR code" }).boundingBox())!;
    const upload = (await page.getByRole("button", { name: "Upload a photo" }).boundingBox())!;
    expect(scan.y).toBe(upload.y);
    expect(scan.width + upload.width).toBeLessThanOrEqual(440);
    expect(scan.height).toBeGreaterThanOrEqual(64);
  });

  test("before connecting there is no sidebar", async ({ page }) => {
    await injectWallet(page, { key: KEY });
    await page.route("**/config", (route) => route.fulfill({ json: { chains: {}, limits: { minInrPaise: "100", maxInrPaise: "100000" } } }));
    await page.goto("/");
    await expect(page.getByRole("button", { name: /^Connect/ })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "RupeeRail" })).toHaveCount(1);
  });
});

test.describe("on a phone", () => {
  test("navigation stays a bottom bar", async ({ page }) => {
    await open(page);
    await expect(page.locator(".MuiBottomNavigation-root")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
    await shot(page, "layout-phone-pay");
  });

  test("an illustration sits above Scan to pay, hidden from screen readers", async ({ page }) => {
    await open(page);
    const art = page.getByTestId("scan-illustration");
    await expect(art).toHaveAttribute("aria-hidden", "true");
    const artBox = (await art.boundingBox())!;
    const heading = (await page.getByRole("heading", { name: "Scan to pay" }).boundingBox())!;
    expect(artBox.y + artBox.height).toBeLessThanOrEqual(heading.y);
    expect(artBox.height).toBeGreaterThan(100);
  });

  test("a list's outer corners are very round, and its inner corners nearly square", async ({ page }) => {
    await open(page);
    await page.getByRole("button", { name: "History", exact: true }).click();
    const rows = page.getByRole("list", { name: "Payments" }).locator("> li > *");
    await expect(rows).toHaveCount(3);
    const corners = await rows.evaluateAll((elements) => elements.map((element) => {
      const style = getComputedStyle(element);
      return [style.borderTopLeftRadius, style.borderBottomRightRadius];
    }));
    expect(corners).toEqual([["28px", "6px"], ["6px", "6px"], ["6px", "28px"]]);
    await shot(page, "layout-phone-history");
  });

  test("a choice of token is a connected group, with its selection tonal", async ({ page }) => {
    await injectWallet(page, { key: KEY });
    const chain = (name: string, symbol: string) => ({ name, chainId: 1, token: { symbol, decimals: 6 }, types: {}, vaultDomain: {} });
    await page.route("**/config", (route) => route.fulfill({
      json: { chains: { arc: chain("Arc", "USDC"), whitechain: chain("Whitechain", "USDT") }, limits: { minInrPaise: "100", maxInrPaise: "100000" } },
    }));
    await page.route("**/account/**", (route) => route.fulfill({ json: { balance: "1", balanceFormatted: "1", symbol: "USDC", permitNonce: "0" } }));
    await page.goto("/");
    await page.getByRole("button", { name: /^Connect/ }).click();
    await page.getByRole("button", { name: "Enter a UPI link" }).click();
    await page.getByLabel("UPI link").fill("upi://pay?pa=shop@ybl");
    await page.getByRole("button", { name: "Continue" }).click();

    const look = (name: RegExp) => page.getByRole("button", { name }).evaluate((element) => {
      const style = getComputedStyle(element);
      return { left: style.borderTopLeftRadius, right: style.borderTopRightRadius, border: style.borderTopWidth, background: style.backgroundColor };
    });
    expect(await look(/USDC/)).toEqual({ left: "28px", right: "6px", border: "0px", background: "rgb(211, 228, 255)" });
    expect(await look(/USDT/)).toEqual({ left: "6px", right: "28px", border: "0px", background: "rgb(248, 249, 255)" });
  });

  test("a secondary button is tonal, not outlined", async ({ page }) => {
    await open(page);
    const upload = page.getByRole("button", { name: "Upload a photo" });
    const background = await upload.evaluate((element) => getComputedStyle(element).backgroundColor);
    expect(background).toBe("rgb(211, 228, 255)");
  });
});
