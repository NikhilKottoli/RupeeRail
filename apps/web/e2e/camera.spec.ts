/** A real camera scan, through Chrome's fake camera playing a video of a QR code. */

import { expect, test } from "@playwright/test";
import path from "node:path";
import { injectWallet } from "./support";

const KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"; // Foundry's public test key 0
const CONFIG = {
  spreadBps: 80, quoteTtlSec: 90,
  limits: { minInrPaise: "100", maxInrPaise: "100000", dailyUserInrPaise: "500000" },
  chains: { arc: { chainId: 5042002, name: "Arc Testnet", rpc: "x", explorer: "x", vault: "x", token: { symbol: "USDC", address: "x", decimals: 6 }, vaultDomain: {}, types: {} } },
};

async function stubApi(page: import("@playwright/test").Page) {
  await page.route(/\/config$/, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(CONFIG) }));
  await page.route(/\/account\//, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ balance: "1", balanceFormatted: "1", symbol: "USDC" }) }));
}

test.use({
  launchOptions: {
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      `--use-file-for-fake-video-capture=${path.join(import.meta.dirname, "fixtures/camera.mjpeg")}`,
    ],
  },
});

test.describe("with a camera", () => {
  test("scans a QR from the camera and releases the camera afterwards", async ({ page }) => {
    await stubApi(page);
    await injectWallet(page, { key: KEY });
    await page.addInitScript(() => {
      const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (constraints) => {
        const stream = await original(constraints);
        // @ts-expect-error test hook
        window.__stream = stream;
        return stream;
      };
    });
    await page.goto("/");
    await page.getByRole("button", { name: "Connect Test Wallet" }).click();
    await page.getByRole("button", { name: "Scan QR code" }).click();
    await expect(page.getByRole("heading", { name: "Fixture Shop" })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByLabel("Amount")).toHaveValue("25.50");
    await expect.poll(() => page.evaluate(() => {
      // @ts-expect-error test hook
      return window.__stream.getTracks().every((track: MediaStreamTrack) => track.readyState === "ended");
    })).toBe(true);
  });
});

test.describe("without camera access", () => {
  test("explains the block and offers another way", async ({ page }) => {
    await stubApi(page);
    await injectWallet(page, { key: KEY });
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        throw new DOMException("denied", "NotAllowedError");
      };
    });
    await page.goto("/");
    await page.getByRole("button", { name: "Connect Test Wallet" }).click();
    await page.getByRole("button", { name: "Scan QR code" }).click();
    await expect(page.getByText("Camera access is blocked")).toBeVisible();
    await page.getByRole("button", { name: "Close scanner" }).click();
    await expect(page.getByRole("button", { name: "Upload a photo" })).toBeVisible();
  });
});
