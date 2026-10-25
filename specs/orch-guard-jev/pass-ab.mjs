import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { G, entries, totalEntries, totalUnique, AGENTS, passA, passB, likelySecret } from "./banc-core.mjs";

function hashCmd(c) { return createHash("sha256").update(c).digest("hex").slice(0,12); }

const results = [];
const byAgent = {};
const byReason = {};
let allowAfterT1 = 0, refuseAfterT1 = 0;
const mutatedByT1 = [];

for (const e of entries) {
  const a = passA(e);
  const b = passB(e.cmd, e.agent);
  byAgent[e.agent] = (byAgent[e.agent] || 0) + 1;
  if (a.reason) { byReason[a.reason] = (byReason[a.reason] || 0) + 1; }
  if (b.verdict === "allow") allowAfterT1++; else refuseAfterT1++;
  if (a.verdict === "refuse" && b.verdict === "allow") mutatedByT1.push({ agent: e.agent, cmd: e.cmd, hash: hashCmd(e.cmd), reason: a.reason });
  results.push({ agent: e.agent, hash: hashCmd(e.cmd), hist: a.verdict, afterT1: b.verdict, reasons: b.reasons });
}

console.log("ENTRIES:", totalEntries);
console.log("UNIQUE_CMDS:", totalUnique);
console.log("BY_AGENT:", JSON.stringify(byAgent));
console.log("AFTER_T1 allow:", allowAfterT1, "refuse:", refuseAfterT1);
console.log("MUTATED_BY_T1 (refusé hist -> allow t1):", mutatedByT1.length);
const uniqueMutated = new Set(mutatedByT1.map(m => m.hash)).size;
console.log("UNIQUE_MODIFIED_BY_T1:", uniqueMutated);
for (const m of mutatedByT1.slice(0, 8)) console.log("  ", m.agent, m.hash, m.reason.slice(0, 60));
