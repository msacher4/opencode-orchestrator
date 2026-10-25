import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { G, entries, passC, likelySecret } from "./banc-core.mjs";

let admissible = 0, inadmissible = 0;
const reasons = {};
const admissibleList = [];

for (const e of entries) {
  const c = await passC(e.cmd, e.agent);
  if (e.agent !== "orchestrator" && e.agent !== "reviewer") { inadmissible++; continue; }
  if (c.admissible) { admissible++; admissibleList.push({ agent: e.agent, hash: hashCmd(e.cmd), cmd: e.cmd }); }
  else { inadmissible++; reasons[c.why || "?"] = (reasons[c.why || "?"] || 0) + 1; }
}

function hashCmd(c) { return createHash("sha256").update(c).digest("hex").slice(0,12); }

console.log("ADMISSIBLE_T2:", admissible);
console.log("INADMISSIBLE_T2:", inadmissible);
console.log("INADMISSIBLE_REASONS:", JSON.stringify(reasons));
console.log("UNIQUE_ADMISSIBLE:", new Set(admissibleList.map(a => a.hash)).size);
for (const a of admissibleList.slice(0, 5)) console.log("  adm:", a.agent, a.hash);
