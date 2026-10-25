# done.md — orch-guard-jev (état au 2026-10-25, nuit)

## Changé
- **t0 ✅** — dump `context-prompts/dumps/plugin-opencode-v2.txt` reconstruit (316 182 o, --check exit 0, last_built 2026-10-24T20:43:35Z).
- **t1 ✅** — `Opencodev2/config/opencode/plugins/orch-guard.ts` déployé en parsing conservateur (1385 lignes, une transaction) : segments/firstWord/mutating quote-aware, READONLY_CMD élargie (env, printf, ps, pgrep, md5sum, sha256sum, sleep, readlink, test, true, false, cd), tar/journalctl/node --check contrôles stricts, parseSteps normalisé (**gras**), exports pour le banc. **131 tests node PASS / 0 FAIL**. Cas historiques : `cd && node` autorisé, `grep '<script>'` autorisé, `git show > /tmp` toujours bloqué, `node -e` toujours bloqué. Requalifications laissées : `command -v`, `gh`.
- **t2 ❌ interrompu par Marc avant toute écriture** — le guard est resté à l'état t1. Reprise prévue en tête du plan de nuit.
- **Écrits pour la nuit** : `specs/orch-guard-jev/spec.md`, `.opencode/night/manifests/orch-guard-jev-night.md` (4 étapes : t2 refait → banc → revue locale → branche locale sans push), file `queue.jsonl` réordonnée (n1 = orch-guard, n2 = polystrata).

## Appris
- Le guard lui-même a faux-refusé 3 fois pendant l'orchestration (`python3` inline, `cmp`, `opencode --help`) — exactement les faux positifs qu'on corrige ; bons cas de test pour t3.
- `opencode run` de cette version **refuse `--prompt`** : le runner de nuit `.opencode/night/run.sh` (ligne 267) échoue code 1 à chaque plan. Run nuit du 25/10 01h51 : n1 et n2 marqués failed sans exécution, config restaurée proprement, verrou retiré — aucun dégât.
- Les workers locaux `llama-proxy/qwen3.8-27b` fonctionnent bien (t0, t1, t4 du jour) ; le flash gratuit cline-pass sature vite (quota).

## Reste (pour demain)
0. **t2 en cours cette nuit** : worker `ses_e8fab6046` (étape n-t2-jev du manifest de nuit) relancé manuellement par Marc à ~02h49 via pause→play. À vérifier le matin : `stat` sur orch-guard.ts (si > 1385 lignes, t2 a déployé) + contrat dans l'audit. L'orchestrateur de nuit étant mort (bug run.sh), AUCUNE étape ne suivra automatiquement après t2 : le banc (t3), la revue (t4) et le commit (t5) restent à lancer.
1. **Corriger `run.sh` ligne 267** : remplacer `--prompt "$prompt"` par la syntaxe de la version installée (voir `opencode run --help` ; passer le prompt en argument positionnel ou via stdin `printf '%s' "$prompt" | opencode run ...`). Puis `bash -n`.
2. **Relancer la nuit** : `cd /media/marcs/Linux_Apps/Projets_AI && nohup bash .opencode/night/run.sh >> .opencode/night/run.log 2>&1 &` — la file n1/n2 est prête et pre-validée.
3. Après le plan : push de la branche `orch/guard-jev` sur `github.com/msacher4/opencode-orchestrator` (interdit la nuit, autorisé le jour), clôture GTD + mémoire Memento.
4. Requalifications humaines t1 : `command -v`, `gh`.
