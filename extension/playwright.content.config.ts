import { defineConfig } from "@playwright/test";

/** The page-reading code alone, on plain test pages. It needs no agent server, so it can run while a real one is up. */
export default defineConfig({
  testDir: "e2e",
  testMatch: "content.spec.ts",
  reporter: "list",
  use: { channel: "chrome" },
});
