#!/usr/bin/env node
// Banc de rejeu t3 — mesure les effets t1+t2 sur les commandes historiques bloquées.
// Node uniquement. Lit l'audit en lecture seule, n'exécute jamais les commandes,
// n'écrit que dans specs/orch-guard-jev/. Mocks explicitement étiquetés.

import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { createHash } from "node:crypto";

// ---------- Chargement du guard dans un contexte VM isolé (fidélité totale) ----------
function loadGuard() {
  const src = readFileSync(
    "/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/orch-guard.ts",
    "utf8"
  );
  let s = src.replace(/^import [^\n]*from "node:[^"]*"\n/mg, "");
  s = s.replace(/\nexport \{ [\s\S]*?\n\}/m, "");
  s = s.replace(/\nexport default \{[\s\S]*\n\}/, "");
  s = s.replace(/^export /mg, "");
  if (/^import /m.test(s)) throw new Error("imports remaining");
  s += "\n__RESULT__ = { check, segments, firstWord, mutating, READONLY_CMD, TEST_RUNNER, cmdOf, jevHash, jevIsUnknownWordRefusal, jevIsArbitraryExecutionWrapper, jevLikelySecret, jevReadApiKey, jevSecondOpinion };";
  const sandbox = createContext({
    require, module: { exports: {} }, exports: {}, console, createHash,
    process, setTimeout, clearTimeout, Date, Object, Array, String, Number, Math,
    RegExp, JSON, Map, Set, Promise, isNaN, isFinite, parseInt, parseFloat,
    encodeURI, decodeURI, fetch: globalThis.fetch,
    resolve: require("node:path").resolve,
    relative: require("node:path").relative,
    dirname: require("node:path").dirname,
    sep: require("node:path").sep,
  });
  runInContext(s, sandbox);
  return sandbox.__RESULT__;
}

const G = loadGuard();

// ---------- Extraction de l'audit ----------
const AUDIT_PATH = "/media/marcs/Linux_Apps/Projets_AI/.opencode/orch-audit.jsonl";
const lines = readFileSync(AUDIT_PATH, "utf8").trim().split("\n");
const AGENTS = new Set(["orchestrator", "reviewer", "worker", "merger"]);

const entries = []; // {agent,tool,cmd,blocked,notes,sessionID,at}
for (const ln of lines) {
  try {
    const e = JSON.parse(ln);
    if (e.tool === "shell" && AGENTS.has(e.agent) && e.blocked && e.blocked.length) {
      entries.push({
        agent: e.agent, tool: e.tool, cmd: e.input || "",
        blocked: e.blocked, notes: e.notes, sessionID: e.sessionID, at: e.at,
      });
    }
  } catch {}
}

const totalEntries = entries.length;
const uniqueCmds = new Map();
for (const e of entries) uniqueCmds.set(e.cmd, (uniqueCmds.get(e.cmd) || 0) + 1);
const totalUnique = uniqueCmds.size;

// ---------- Passe A : verdict historique (le refus enregistré) ----------
function passA(e) {
  // Verdict historique = refus (bloqué). Motif principal = premier élément de blocked.
  return { verdict: "refuse", reason: e.blocked[0] || "inconnu" };
}

// ---------- Passe B : verdict déterministe après t1 ----------
function passB(cmd, agent) {
  const r = G.check({ agent, tool: "shell", input: { command: cmd }, sessionID: "bench-t3" });
  return { verdict: r.blocked.length ? "refuse" : "allow", reasons: r.blocked, notes: r.notes };
}

// ---------- Détection secret ----------
function likelySecret(cmd) {
  return G.jevLikelySecret(cmd);
}

// ---------- Passe C : décision Typesafe (zone grise uniquement) ----------
async function passC(cmd, agent) {
  // Admissible seulement si refus d'inconnu admissible pour orchestrator/reviewer
  if (agent !== "orchestrator" && agent !== "reviewer") return { admissible: false };
  if (likelySecret(cmd)) return { admissible: false, why: "secret" };
  if (G.jevIsArbitraryExecutionWrapper(cmd)) return { admissible: false, why: "wrapper" };
  const det = G.check({ agent, tool: "shell", input: { command: cmd }, sessionID: "bench-t3" });
  if (!G.jevIsUnknownWordRefusal(agent, det.blocked)) return { admissible: false, why: "not-unknown-refusal" };
  return { admissible: true };
}

export { G, entries, totalEntries, totalUnique, uniqueCmds, AGENTS, passA, passB, passC, likelySecret };
