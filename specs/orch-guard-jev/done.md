# done.md — orch-guard-jev (clôture 2026-10-25)

## Changé

### Passe 1 — parsing + second avis (commit `5111346`)

- **t0** — dump `context-prompts/dumps/plugin-opencode-v2.txt` reconstruit (316 182 o, `--check` exit 0).
- **t1** — parsing conservateur : quote-aware, `READONLY_CMD` élargie, tar/journalctl stricts, `parseSteps` normalisé, exports pour le banc. 131 tests PASS.
- **t2e** — second avis Typesafe `jev-latest` dans `execute.before` : déclencheur unique (refus premier mot inconnu, `orchestrator`/`reviewer`), validation stricte, cache 24 h (SHA-256 + agent + version), timeout 2500 ms sans retry, fail-closed.
- **t3/t3b** — banc de rejeu : 884 entrées / 849 uniques ; après t1, 443 autorisées / 441 refusées / 429 modifiées ; latence réseau p50 152 ms / p95 170 ms.
- **t4→t4c** — revue indépendante, FIX documentaire, re-revue PASS.
- **t5f** — commit `5111346` (13 fichiers, 3105 insertions) + `.gitignore` corrigé (le motif `opencode` masquait tout `config/opencode/`).

### Passe 2 — second avis réellement opérationnel (commit `b68b13f`)

Le second avis était branché mais **mort** : 37 consultations en audit, 100 % en `fallback-http` (404), zéro décision du modèle. Trois causes dans le code :

1. `jevEndpoint()` / `jevModel()` ne lisaient que `process.env`, alors que `TYPESAFE_URL` n'existe pas dans l'environnement du serveur OpenCode → repli sur `https://api.typesafe.ai/v1/system1` (domaine qui ne sert pas cette API) → **aucun appel n'atteignait OpenRouter**.
2. Modèle par défaut `jev-latest` au lieu de `typesafe/jev-v1.13`.
3. Le paramètre `cmd` n'était **jamais** mis dans le payload : le modèle aurait classé du vide.

Corrections (t7) : endpoint `https://openrouter.ai/api/alpha/decisions`, modèle `typesafe/jev-v1.13`, helper `jevReadEnvFile()` + replis fichier, `state.page.text = String(cmd || "").slice(0, 2000)`.

Puis (t10, décision Marc) : `JEV_CONFIDENCE_MIN` 0,90 → **0,75**, `JEV_PROBA_MIN` maintenu à 0,90. Mesure : l'API rend `confidence` 0,80-0,84 sur un cas trivial, un seuil à 0,90 rendait le second avis incapable de conclure.

### Passe 3 — veto dur sur les vecteurs d'exécution (commit `ed2235e`)

Tests live du 2026-10-25 : le guard était **plus permissif qu'avant** sur deux commandes mutantes.

| Test | Résultat avant patch |
|---|---|
| `awk 'BEGIN{system("touch …")}'` | **autorisée** — le modèle l'a classée « lecture » (`via:"api"`, 568 ms), le `touch` a tourné |
| `find … -delete` | **autorisée** — argument jamais inspecté, aucune consultation |
| `truncate`, `install`, `sed -i` | refusées par les règles dures (correct) |
| `lsblk`, `ls … \| head`, `ps aux \| head` | autorisées (correct) |

Correctif : veto dur placé en tête de `mutating()` —
- `system(`, `exec(`, `execSync(`, `popen(`, `spawn(`, `child_process`, `os.system`, `Runtime.getRuntime` → « exécution de code » ;
- `-delete`, `-exec`, `-execdir`, `-ok`, `-okdir`, `-fprint`, `-fprintf`, `-fls`, `-output` → « argument destructeur » ;
- `sort -o`, `xargs` → motifs dédiés.

Ces motifs **ne déclenchent jamais** la consultation du modèle (`jevIsUnknownWordRefusal` n'accepte que le préfixe « premier mot inconnu »). Bonus : bug préexistant de `tokenize` corrigé (`has` non propagé dans les régions citées, qui fusionnait `'x' -delete` en un token et rendait le veto aveugle).

## Preuves en conditions réelles

| Commande | Résultat | Audit |
|---|---|---|
| `lscpu \| head -3` | exécutée | `verdict:"lecture", via:"api", 530 ms` |
| `lsblk` | exécutée | autorisée (lecture) |
| `shred -n1 …` | refusée | `via:"fallback-invalid"` → repli code |
| `rm -rf …` | refusée | aucun appel API |
| `awk 'BEGIN{system(…)}'` | refusée « exécution de code » | **aucun appel API** |
| `find … -delete` | refusée « argument destructeur » | **aucun appel API** |

## Appris

- **Un fail-closed peut masquer une panne totale** : 100 % des entrées d'audit étaient « refusées », donc rassurantes, alors que le second avis ne fonctionnait pas. `via:fallback-*` à 100 % = alerte, pas preuve de sûreté.
- **200 ≠ contrat respecté** : séparer `fallback-http` (transport) de `fallback-invalid` (validation) a été décisif pour le diagnostic.
- **Un modèle n'est pas une frontière de sécurité** : Typesafe a classé `awk system()` comme lecture. Tout vecteur d'exécution doit être refusé par le code, avant tout appel.
- Les variables d'env du serveur OpenCode et le `.env` d'un outil tiers sont deux mondes ; un défaut codé en dur doit être confronté à la config réelle.
- ID nvidia instables : le « nemotron 3 ultra 550b » n'existe plus ; seul `nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b` reste (+ variantes free opencode/openrouter).
- Gotchas guard : statut de carte devant valoir exactement `annoncée` ; un prompt contenant le chemin du manifest de la session est refusé (« manifest explicite non autorisé ») ; un merger n'exécute que git.
- Un contrat de worker peut être faux (lecture `wc` erronée) : le frontière revérifie toujours.

## Reste

1. **Vecteurs résiduels à documenter** : `sed 'w /tmp/x'` (écriture via commande `w` du script sed, non couverte par le veto qui ne vise que `sed -i`), `perl -e` et `python -c` lancés **par un worker** (le veto couvre le frontière ; le worker a déjà le droit de muter).
2. **Faiblesse préexistante (gravité moyenne)** : le shell d'un worker peut `rm`/`mv`/`chmod` hors `.opencode/night/` (`orch-guard.ts` ~l.1557-1572). Source des 3 FAIL permanents de `non-regression.mjs`. Chantier séparé.
3. **Requalification humaine** : `command -v` (hash `7c00406c2ce2`) et `gh` (hash `fb2b7fce0940`).
4. `config/polystrata/settings.json` modifié dans le repo, hors périmètre — à trier séparément.
5. Le seuil 0,75 est un premier calibrage : à réévaluer quand l'API rendra des confiances plus franches.
6. **Archive du manifest impossible** via subagent (bug guard documenté) : à faire à la main (`cp .opencode/orch/<session>/orchestration.md .opencode/orch-archive/`).

## Commits

- `5111346` — parsing conservateur + second avis Typesafe + banc de rejeu.
- `b68b13f` — second avis JEV opérationnel (URL OpenRouter, payload commande, seuil 0.75).
- `f9c7245` — bilan `done.md`.
- `ed2235e` — veto dur sur les vecteurs d'exécution (awk system, find -delete, sort -o, xargs).
- Branche `orch/guard-jev` poussée sur `origin` (`msacher4/opencode-orchestrator`).
