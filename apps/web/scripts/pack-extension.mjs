/** Builds extension/ and zips it into public/downloads, where the Agents Hub offers it. Runs before every web build. */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const web = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const extension = resolve(web, "../../extension");
const pack = resolve(web, "public/downloads/rupeerail-agents-extension.zip");

if (!existsSync(resolve(extension, "node_modules"))) execFileSync("npm", ["ci"], { cwd: extension, stdio: "inherit" });
execFileSync("npm", ["run", "build"], { cwd: extension, stdio: "inherit" });

mkdirSync(dirname(pack), { recursive: true });
rmSync(pack, { force: true }); // zip adds to an existing archive, which would keep files the extension no longer has
// The files sit at the top level, so the folder a person unzips is the one Chrome's Load unpacked wants.
execFileSync("zip", ["-q", "-X", "-r", pack, "."], { cwd: resolve(extension, "dist"), stdio: "inherit" });
