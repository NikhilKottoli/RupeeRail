import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  workers: 1,
  reporter: "list",
  webServer: {
    command: "cd ../agent-server && uv run python -m tests.e2e_stack",
    url: "http://127.0.0.1:5181/",
    reuseExistingServer: false,
    env: { ...process.env } as Record<string, string>,
    timeout: 90_000,
  },
});
