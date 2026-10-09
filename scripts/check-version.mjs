// Fails when the version isn't the same everywhere the build and the updater read it
// (the installer gets it from package.json at build time),
// or when its release notes are missing. Run by CI; also `node scripts/check-version.mjs`.
import { existsSync, readFileSync } from "node:fs";

const root = new URL("../", import.meta.url);
const read = (file) => readFileSync(new URL(file, root), "utf-8");
const match = (file, pattern) => read(file).match(pattern)?.[1] ?? "(not found)";

const version = JSON.parse(read("package.json")).version;
const lock = JSON.parse(read("package-lock.json"));
const places = {
  "package-lock.json": lock.version,
  'package-lock.json packages[""]': lock.packages?.[""]?.version,
  "backend/codex_engine/config.py APP_VERSION": match("backend/codex_engine/config.py", /^APP_VERSION\s*=\s*"([^"]+)"/m),
};

const problems = Object.entries(places)
  .filter(([, found]) => found !== version)
  .map(([place, found]) => `${place} is ${found}, but package.json is ${version}`);
if (!existsSync(new URL(`release-notes-${version}.txt`, root))) problems.push(`release-notes-${version}.txt is missing`);

if (problems.length) {
  console.error(problems.map((p) => `- ${p}`).join("\n"));
  process.exit(1);
}
console.log(`Version ${version} matches everywhere, and its release notes exist.`);
