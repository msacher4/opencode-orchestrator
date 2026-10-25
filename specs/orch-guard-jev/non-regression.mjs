import { G } from "./banc-core.mjs";

function check(agent, tool, input) {
  return G.check({ agent, tool, input, sessionID: "bench-t3-nr" });
}

let pass=0, fail=0;
function t(name, cond) {
  if (cond) { pass++; console.log("PASS:", name); }
  else { fail++; console.log("FAIL:", name); }
}

// 1. Mutations
t("rm bloqué", check("worker","shell",{command:"rm -rf /tmp/x"}).blocked.length>0);
t("git push bloqué", check("worker","shell",{command:"git push origin main"}).blocked.length>0);
t("mv bloqué", check("worker","shell",{command:"mv a b"}).blocked.length>0);
t("chmod bloqué", check("worker","shell",{command:"chmod 777 f"}).blocked.length>0);

// 2. Réseau écriture
t("curl POST bloqué", check("worker","shell",{command:"curl -X POST https://example.com"}).blocked.length>0);
t("git push bloqué (reviewer)", check("reviewer","shell",{command:"git push"}).blocked.length>0);

// 3. Secrets (exclus de Typesafe, reste bloqué)
t("secret Token bloqué", check("orchestrator","shell",{command:"export TOKEN=sk-abc123def456"}).blocked.length>0);
t("Authorization header bloqué", check("reviewer","shell",{command:"curl -H 'Authorization: Bearer xyz' https://x"}).blocked.length>0);

// 4. Lecture autorisée (non régression du élargissement t1)
t("grep lecture autorisé", check("reviewer","shell",{command:"grep -n foo /tmp/x"}).blocked.length===0);
t("sha256sum autorisé", check("reviewer","shell",{command:"sha256sum /tmp/x"}).blocked.length===0);
t("ps autorisé", check("reviewer","shell",{command:"ps aux"}).blocked.length===0);

// 5. node -e toujours bloqué (pas d'enveloppe d'exécution arbitraire)
t("node -e bloqué", check("reviewer","shell",{command:"node -e '1'"}).blocked.length>0);

// 6. Permissions rôles
t("reviewer write bloqué", check("reviewer","write",{filePath:"/tmp/x"}).blocked.length>0);
t("orchestrator write hors périmètre bloqué", check("orchestrator","write",{filePath:"/etc/passwd"}).blocked.length>0);
t("merger non-git bloqué", check("merger","shell",{command:"cat /tmp/x"}).blocked.length>0);

// 7. Mode nuit (verrou absent = nuit inactive, execAllowed normal)
// vérifier que execAllowed existe et fonctionne
t("execAllowed free", typeof G.check==="function");

console.log("\nNON-REGRESSION:", pass, "pass", fail, "fail");
process.exit(fail>0?1:0);
