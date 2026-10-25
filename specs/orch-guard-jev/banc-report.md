# Banc de rejeu — rapport chiffré orch-guard-jev (t3b)

Source : `specs/orch-guard-jev/` (scripts + `real-api-results.json`). Simulation pure : aucune commande de l'audit n'a été exécutée, aucun nouvel appel API.

## 1. Méthode

Trois passes, lecture seule sur l'audit `.opencode/orch-audit.jsonl` :

- **Passe A** : verdict historique (refus enregistré, motif = premier élément de `blocked`).
- **Passe B** : verdict déterministe après t1 via `G.check` sur le guard déployé (1385 lignes). `allow` = refus historique devenu légal.
- **Passe C** : décision Typesafe `jev-latest` sur la zone grise (orchestrator/reviewer, refus inconnu, pas secret, pas wrapper). Appels HTTP réels déjà collectés dans `real-api-results.json` (46 Ko). Aucun nouvel appel.

Replis testés (mock, pas d'API) : clé absente, timeout, HTTP, invalide → verdict code conservé.

Non mesuré : coût (API 404, aucune donnée d'usage, aucun tarif vérifiable).

## 2. Nombres bruts

| Indicateur | Valeur | Source |
|---|---|---|
| Entrées rejouées (toutes passes) | 884 | `pass-ab.mjs` (re-exécuté) |
| dont orchestrator | 138 | `pass-ab.mjs` (byAgent) |
| dont reviewer | 640 | `pass-ab.mjs` (byAgent) |
| dont worker | 77 | `pass-ab.mjs` (byAgent) |
| dont merger | 29 | `pass-ab.mjs` (byAgent) |
| Commandes uniques | 849 | `pass-ab.mjs` (re-exécuté) |
| Passe B — autorisées après t1 | 443 | `pass-ab.mjs` (re-exécuté) |
| Passe B — encore refusées après t1 | 441 | `pass-ab.mjs` (re-exécuté) |
| Commandes uniques modifiées par t1 (refusé hist → allow) | 429 | `pass-ab.mjs` (re-exécuté) |
| Passe C — entrées candidates | 122 | `pass-c-count.mjs` (re-exécuté) |
| Passe C — uniques candidates | 119 | `pass-c-count.mjs` (re-exécuté) |
| Appels API réels | 117 | `real-api-results.json` (collecte t3, aucun nouvel appel) |
| dont HTTP 404 | 117 | `real-api-results.json` `httpErr` |
| dont succès Typesafe | 0 | `real-api-results.json` `okCount` |

Répartition des irrecevables Passe C (raison d'irrecevabilité, sur 884 entrées) :
- non-unknown-refusal : 492
- wrapper : 151
- secret : 13
- worker/merger (hors orchestrator/reviewer) : 106
Total : 762 ; 884 − 762 = 122 admissibles en entrée → 119 uniques.

Latence réseau réelle (117 appels, ms) : p50 152, p95 170, min 140, max 307. Tous HTTP 404.
Cache (50 appels simulés) : p50 0 ms, p95 1 ms.
Mock (50 appels simulés) : 0 ms.

## 3. Tableau avant/après t1

| État | Refus | Autorisés | Total |
|---|---|---|---|
| Historique (Passe A) | 884 | 0 | 884 |
| Après t1 (Passe B) | 441 | 443 | 884 |
| Commandes uniques modifiées par t1 | — | 429 (sur 849) | — |

Les 443 autorisées = refus historiques devenus légaux par le parsing conservateur t1 (segments/firstWord/mutating quote-aware, READONLY_CMD élargie).

## 4. Passe C — zone grise

- 119 candidats uniques (pass-c-count re-exécuté sur l'audit actuel).
- 0 décision Typesafe obtenue : 117 appels déjà collectés, tous HTTP 404 (contrat OpenRouter incompatible avec l'endpoint Typesafe `https://api.typesafe.ai/v1/system1` ; aucun nouvel appel).
- Passe réelle **INCOMPLÈTE**, assumée. Aucun bénéfice t2 réel tant que l'endpoint n'est pas corrigé.
- Repli : verdict code conservé (déterministe t1) pour tous les candidats.

## 5. Replis (testés OK, mock — aucune API)

| Scénario | Résultat | Test |
|---|---|---|
| Clé absente (`jevReadApiKey → null`) | verdict identique à t1 | `fallback-tests.mjs` KEY_ABSENT |
| Timeout (mock injection AbortError) | verdict identique à t1 | `fallback-tests.mjs` TIMEOUT |
| HTTP (réel, 404 × 117) | verdict code conservé | `real-api-results.json` |
| Invalide | verdict code conservé | repli générique du guard |

## 6. Latences (séparées)

| Mesure | p50 | p95 | Note |
|---|---|---|---|
| Réseau réel (117 appels) | 152 ms | 170 ms | tous HTTP 404 |
| Cache (50 appels simulés) | 0 ms | 1 ms | `latency-tests.mjs` |
| Mock (50 appels simulés) | 0 ms | 0 ms | `latency-tests.mjs` |

## 7. Coût

**Non mesurable.** Raison : 117 appels HTTP 404, aucune donnée d'usage retournée par Typesafe, aucun tarif vérifiable. Pas de simulation de coût introduite.

## 8. Limites

- Aucun oracle indépendant disponible → faux refus confirmés = 0, marqués **INDÉTERMINÉ** (ne jamais prétendre le contraire).
- Endpoint Typesafe à corriger (`https://api.typesafe.ai/v1/system1` renvoie 404) avant tout bénéfice t2 réel.
- Passe C incomplète : 0 décision sur 119 candidats.
- Coût non mesurable (voir §7).

## 9. Points nécessitant requalification humaine

(Hashes/extraits assainis uniquement — jamais de commande brute.)

- `command -v` (hash assaini : `7c00406c2ce2`) — requalification laissée en suspens t1 (`done.md`).
- `gh` (hash assaini : `fb2b7fce0940`) — requalification laissée en suspens t1 (`done.md`).
- 3 faux refus observés pendant l'orchestration (`python3` inline, `cmp`, `opencode --help`) — bons cas de test t3, à requalifier.

## 10. Non-régression (inchangés)

| Domaine | Statut |
|---|---|
| Mutations (rm, mv, chmod) | worker : NON bloqués hors `.opencode/night/` (état réel, non-regression.mjs : 3 FAIL sur rm/mv/chmod worker) ; git push : bloqué — inchangé |
| Réseau écriture (curl POST, git push reviewer) | bloqués — inchangés |
| Secrets (Token, Authorization header) | exclus de Typesafe, restent bloqués — inchangés |
| Lecture autorisée (grep, sha256sum, ps) | autorisés — inchangés |
| `node -e` | bloqué (pas d'enveloppe d'exécution arbitraire) — inchangé |
| Rôles (reviewer write, orchestrator write hors périmètre, merger non-git) | bloqués — inchangés |
| Mode nuit (verrou) | execAllowed normal — inchangé |
| Tests node | 131 PASS / 0 FAIL (t1) — inchangés |

Détail : `specs/orch-guard-jev/non-regression.mjs`.

## 11. Faiblesses préexistantes signalées (hors périmètre)

Le worker shell exécute des commandes mutantes (rm, mv, chmod) hors `.opencode/night/` sans blocage par le guard. Faiblesse préexistante au baseline t0, non introduite par t1+t2. Gravité : moyenne. Signalée au propriétaire ; non corrigée dans le cadre de cette orchestration (hors périmètre de t4b).

---
Généré par le worker t4b. Aucun fichier hors `specs/orch-guard-jev/banc-report.md` n'a été modifié.
