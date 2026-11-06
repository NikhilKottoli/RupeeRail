/** The small things that are easy to lose: a press that answers at once, and motion that respects the person's setting. */

import { expect, test, type Page } from "@playwright/test";
import { injectWallet } from "./support";

const KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // Foundry's public test key

async function open(page: Page) {
  await injectWallet(page, { key: KEY });
  await page.route("**/config", (route) => route.fulfill({ json: { chains: {}, limits: { minInrPaise: "100", maxInrPaise: "100000" } } }));
  await page.goto("/");
  return page.getByRole("button", { name: /^Connect/ });
}

async function pressedTransform(page: Page, button: ReturnType<Page["getByRole"]>) {
  const box = (await button.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(250);
  const transform = await button.evaluate((element) => getComputedStyle(element).transform);
  await page.mouse.move(0, 0);
  await page.mouse.up();
  return transform;
}

test("a button answers a press, with no ripple", async ({ page }) => {
  const connect = await open(page);
  await expect(connect).toBeVisible();
  await page.waitForTimeout(500); // the first screen's entrance
  expect(await pressedTransform(page, connect)).toContain("matrix(0.97");
  await expect(connect.locator(".MuiTouchRipple-root")).toHaveCount(0);
});

test("with reduced motion, nothing moves on a press or on entry", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const connect = await open(page);
  await expect(connect).toBeVisible();
  expect(await pressedTransform(page, connect)).toBe("none");
  const animation = await page.locator(".rr-stagger > *").first().evaluate((element) => getComputedStyle(element).animationName);
  expect(animation).toBe("none");
});

test("the Buy tab can be left out of a build", async ({ page }) => {
  test.skip(process.env.E2E_BUY_TAB !== "off", "only for a build made with VITE_BUY_TAB=off");
  await injectWallet(page, { key: KEY });
  await page.route("**/config", (route) => route.fulfill({ json: { chains: {}, limits: { minInrPaise: "100", maxInrPaise: "100000" } } }));
  await page.goto("/");
  await page.getByRole("button", { name: /^Connect/ }).click();
  await expect(page.getByRole("button", { name: "Pay", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "History", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Buy", exact: true })).toHaveCount(0);
});
