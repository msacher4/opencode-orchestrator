# done.md — orch-guard-jev (clôture 2026-10-25)

## Changé

### Jour 1 — parsing + second avis (commit `5111346`)

- **t0** — dump `context-prompts/dumps/plugin-opencode-v2.txt` reconstruit (316 182 o, `--check` exit 0).
- **t1** — parsing conservateur : quote-aware, `READONLY_CMD` élargie, tar/journalctl stricts, `parseSteps` normalisé, exports pour le banc. 131 tests PASS.
- **t2e** — second avis Typesafe `jev-latest` dans `execute.before` : déclencheur unique (refus premier mot inconnu, `orchestrator`/`reviewer`), validation stricte, cache 24 h (SHA-256 + agent + version), timeout 2500 ms sans retry, fail-closed.
- **t3/t3b** — banc de rejeu chiffré : 884 entrées / 849 uniques ; après t1, 443 autorisées / 441 refusées / 429 modifiées ; latence réseau p50 152 ms / p95 170 ms.
- **t4→t4c** — revue indépendante, FIX documentaire, re-revue PASS.
- **t5f** — commit `5111346` (13 fichiers, 3105 insertions), `.gitignore` corrigé (le motif `opencode` masquait tout `config/opencode/`).

### Jour 2 — le second avis rendu réellement opérationnel (commit `b68b13f`)

Le second avis était **branché mais mort** : 37 consultations dans l'audit, 100 % en `fallback-http` (HTTP 404), donc zéro décision du modèle. Trois causes, toutes dans le code :

1. `jevEndpoint()` / `jevModel()` ne lisaient que `process.env`, alors que `TYPESAFE_URL` n'existe pas dans l'environnement du serveur OpenCode → repli sur `https://api.typesafe.ai/v1/system1` (domaine qui ne sert pas cette API) → **404, aucun appel n'atteignait OpenRouter**.
2. Le modèle par défaut était `jev-latest` au lieu de `typesafe/jev-v1.13`.
3. Le paramètre `cmd` n'était **jamais** inséré dans le payload : même avec la bonne URL, le modèle aurait classé du vide.

Corrections (t7) : endpoint `https://openrouter.ai/api/alpha/decisions`, modèle `typesafe/jev-v1.13`, helper `jevReadEnvFile()` + replis fichier dans `jevEndpoint()`/`jevModel()`, et `state.page.text = String(cmd || "").slice(0, 2000)` dans le corps de requête.

Puis (t10, décision Marc) : `JEV_CONFIDENCE_MIN` 0,90 → **0,75**, `JEV_PROBA_MIN` maintenu à 0,90. Justification mesurée : l'API renvoie `confidence` 0,80-0,84 sur un cas trivial (`lscpu`), donc un seuil à 0,90 rendait le second avis structurellement incapable de conclure.

## Preuves en conditions réelles (2026-10-25, 13h04 → 13h14)

| Commande | Attendu | Résultat | Audit |
|---|---|---|---|
| `lscpu \| head -3` (zone grise bénigne) | consulté puis autorisé | **exécutée** | `verdict:"lecture", via:"api", 530 ms` |
| `shred -n1 …` (zone grise dangereuse) | bloqué | **refusée** | `via:"fallback-invalid"` → repli code |
| `rm -rf …` (règle dure) | bloqué sans consulter | **refusée** | **aucun appel API** (compteur `jev` stable) |

Sonde directe (t9, `/tmp` only, aucune clé affichée) : 2 runs HTTP 200 `application/json`, `answers.classification = {choice:"lecture", probabilities:{lecture:0.92|0.90, mutation:0.08|0.10}, confidence:0.84|0.80}`.

## Appris

- **Un fail-closed peut masquer une panne complète** : toutes les entrées d'audit étaient « refusées », donc rassurantes, alors que le second avis ne fonctionnait pas du tout. Un `via:fallback-*` à 100 % est un signal d'alerte, pas une preuve de sûreté.
- Les variables d'environnement du **serveur** OpenCode et celles du `.env` d'un outil tiers sont deux mondes : tout défaut codé en dur doit être confronté à la config réelle avant d'être déclaré « déployé ».
- Une réponse HTTP 200 ne vaut pas contrat respecté : séparer `fallback-http` (transport) de `fallback-invalid` (validation) a été décisif pour le diagnostic.
- Les ID de modèles nvidia bougent (le « nemotron 3 ultra 550b » n'existe plus dans le catalogue ; seul `nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b` reste) : vérifier le catalogue avant d'épingler.
- Slots guard fantômes (spawn annulé, session tuée, modèle retiré) : contournement = clôturer la carte au manifest et ouvrir un id neuf (t12→t12b, t13→t13b).
- Le statut d'une carte doit valoir exactement `annoncée` : un suffixe (`annoncée (en attente…)`) fait considérer l'étape comme inexistante par le parser.

## Reste

1. **Faiblesse préexistante (gravité moyenne)** : le shell d'un worker peut exécuter `rm`/`mv`/`chmod` hors `.opencode/night/` (code `orch-guard.ts` ~l.1557-1572, « Mutating command OUTSIDE .opencode/night/ → allowed »). Les 3 FAIL de `non-regression.mjs` viennent de là. Chantier séparé.
2. **Requalification humaine** : `command -v` (hash `7c00406c2ce2`) et `gh` (hash `fb2b7fce0940`).
3. **Push GitHub** : nécessite des identifiants (remote HTTPS + `credential.helper=store`, aucun `~/.git-credentials`, clé SSH `id_ed25519` présente). Marc a fourni un token le 2026-10-25.
4. `config/polystrata/settings.json` modifié dans le repo, hors périmètre de ce chantier — à trier séparément.
5. Le seuil 0,75 est un premier calibrage : à réévaluer quand l'API Typesafe rendra des confiances plus franches (observer la distribution réelle sur plusieurs semaines).

## Commits

- `5111346` — orch-guard : parsing conservateur + second avis Typesafe + banc de rejeu.
- `b68b13f` — orch-guard : second avis JEV opérationnel (URL OpenRouter, payload commande, seuil confiance 0.75).
- Branche `orch/guard-jev` poussée sur `origin` (`msacher4/opencode-orchestrator`).
