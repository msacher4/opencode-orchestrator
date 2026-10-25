import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import { G, entries, likelySecret } from "./banc-core.mjs";

// Candidats admissibles uniques (orchestrator/reviewer, refus inconnu, pas secret, pas wrapper)
const seen = new Map();
for (const e of entries) {
  if (e.agent !== "orchestrator" && e.agent !== "reviewer") continue;
  if (likelySecret(e.cmd)) continue;
  if (G.jevIsArbitraryExecutionWrapper(e.cmd)) continue;
  const r = G.check({ agent: e.agent, tool: "shell", input: { command: e.cmd }, sessionID: "bench-t3" });
  if (G.jevIsUnknownWordRefusal(e.agent, r.blocked)) {
    if (!seen.has(e.cmd)) seen.set(e.cmd, e.agent);
  }
}
const candidates = [...seen.entries()].map(([cmd, agent]) => ({
  cmd, agent, hash: createHash("sha256").update(cmd).digest("hex").slice(0,12)
}));
console.log("UNIQUE_ADMISSIBLE_FOR_REAL_API:", candidates.length);

// Clé API (jamais affichée)
const keyRaw = readFileSync("/media/marcs/Linux_Apps/jev-ultrafast/.env","utf8");
const m = keyRaw.match(/^TYPESAFE_API_KEY\s*=\s*(.+)$/m);
const key = m ? m[1].trim().replace(/^["']|["']$/g,"") : "";

const results = [];
let okCount = 0, httpErr = 0, timeout = 0, networkErr = 0;
for (const c of candidates) {
  const t0 = Date.now();
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2500);
    const body = {
      model: "jev-latest",
      state: { page: { url: "", title: "", text: "" }, elements: [], recent_actions: [] },
      questions: { classification: { type: "choice", criteria: { lecture: "read", mutation: "mutate" }, instructions: { goal: "classify", rules: [] } } }
    };
    const resp = await fetch("https://api.typesafe.ai/v1/system1", {
      method: "POST", headers: { "Content-Type": "application/json", "Authorization": "Bearer " + key },
      body: JSON.stringify(body), signal: controller.signal,
    });
    clearTimeout(timer);
    const lat = Date.now() - t0;
    if (!resp.ok) { httpErr++; results.push({ ...c, via: "http", status: resp.status, latency: lat }); }
    else { okCount++; const j = await resp.json(); results.push({ ...c, via: "api", latency: lat, choice: j.answers?.classification?.choice }); }
  } catch (e) {
    clearTimeout(timer);
    const lat = Date.now() - t0;
    if (e.name === "AbortError") { timeout++; results.push({ ...c, via: "timeout", latency: lat }); }
    else { networkErr++; results.push({ ...c, via: "network", latency: lat }); }
  }
}
console.log("REAL_API ok:", okCount, "httpErr:", httpErr, "timeout:", timeout, "networkErr:", networkErr);
writeFileSync("./specs/orch-guard-jev/real-api-results.json", JSON.stringify({ candidates: candidates.length, okCount, httpErr, timeout, networkErr, results }, null, 2));
console.log("écrit specs/orch-guard-jev/real-api-results.json");
