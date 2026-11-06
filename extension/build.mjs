import { build } from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";

rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });
cpSync("static", "dist", { recursive: true });
await build({
  entryPoints: ["src/background.ts", "src/content.ts", "src/panel.ts"],
  bundle: true,
  format: "iife",
  target: "chrome120",
  outdir: "dist",
  logLevel: "warning",
});
