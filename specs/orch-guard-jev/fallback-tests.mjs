import { readFileSync } from "node:fs";
import { G, entries, passB } from "./banc-core.mjs";

// Trouver une commande admissible (zone grise) pour tester fallback
let testCmd = null, testAgent = null;
const seen = new Set();
for (const e of entries) {
  if (e.agent !== "orchestrator" && e.agent !== "reviewer") continue;
  if (seen.has(e.cmd)) continue; seen.add(e.cmd);
  if (G.jevLikelySecret(e.cmd)) continue;
  if (G.jevIsArbitraryExecutionWrapper(e.cmd)) continue;
  const r = G.check({ agent: e.agent, tool: "shell", input: { command: e.cmd }, sessionID: "bench-t3" });
  if (G.jevIsUnknownWordRefusal(e.agent, r.blocked)) { testCmd = e.cmd; testAgent = e.agent; break; }
}
console.log("TEST_CMD:", testCmd, "| AGENT:", testAgent);

// Référence déterministe (t1)
const det = G.check({ agent: testAgent, tool: "shell", input: { command: testCmd }, sessionID: "bench-t3" });
console.log("DET:", JSON.stringify(det));

// ---- TEST 1 : clé absente (mock jevReadApiKey -> null) ----
const origReadKey = G.jevReadApiKey;
G.jevReadApiKey = () => null;
const r1 = await G.jevSecondOpinion({ agent: testAgent, tool: "shell", input: { command: testCmd }, sessionID: "bench-t3" }, det);
console.log("KEY_ABSENT (mock) identical to det:", JSON.stringify(r1) === JSON.stringify(det) ? "OUI" : "NON");
console.log("KEY_ABSENT result:", JSON.stringify(r1));
// restaurer
G.jevReadApiKey = origReadKey;

// ---- TEST 2 : timeout (mock fetch qui abort) ----
const origFetch = globalThis.fetch;
globalThis.fetch = async () => { const c = new AbortController(); setTimeout(() => c.abort(), 50); return c.signal; };
// On ne peut pas vraiment faire abort via fetch retournant signal. Simulons à la place en surchargeant jevCallApi
const origCallApi = G.jevCallApi;
G.jevCallApi = async () => { await new Promise(r=>setTimeout(r,10)); throw Object.assign(new Error("timeout"), { name: "AbortError" }); };
const r2 = await G.jevSecondOpinion({ agent: testAgent, tool: "shell", input: { command: testCmd }, sessionID: "bench-t3" }, det);
console.log("TIMEOUT (mock) identical to det:", JSON.stringify(r2) === JSON.stringify(det) ? "OUI" : "NON");
console.log("TIMEOUT result:", JSON.stringify(r2));
G.jevCallApi = origCallApi;
globalThis.fetch = origFetch;

console.log("FALLBACK_TESTS_DONE");
