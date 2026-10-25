# Orchestration — Finalisation et validation de l’outil MCP `act` de `cloak-agent`

## Étapes

### t0 — PROPOSAL `mcp-browser-automation` (historique)
- **id** : t0
- **Qui** : worker
- **Périmètre** : Conserver le PROPOSAL déjà créé dans `.opencode/context-prompts/PROPOSAL-mcp-browser-automation.json`. Ne pas réécrire ni recréer ce fichier sauf incohérence explicitement constatée pendant l’audit.
- **Statut** : terminée
- **Prompt** :

```text
NE PAS EXÉCUTER. Étape déjà terminée. Ne modifie aucun fichier. Conserve le PROPOSAL existant comme trace et laisse sa vérification à l’audit si une incohérence apparaît. Retourne uniquement : « étape déjà terminée, aucune modification ».
```

### t1 — Dump d’architecture `mcp-browser-automation` (historique)
- **id** : t1
- **Qui** : worker
- **Périmètre** : Conserver le dump existant `dumps/mcp-browser-automation.txt` et son entrée `MANIFEST.json`. Ne pas reconstruire le dump à moins que l’audit démontre qu’il est obsolète ou que le mode `--check` échoue.
- **Statut** : terminée
- **Prompt** :

```text
NE PAS EXÉCUTER. Étape déjà terminée. Ne relance pas le script de génération et ne modifie ni le dump ni MANIFEST.json sans preuve que le dump ne correspond plus aux sources actuelles. Retourne uniquement : « étape déjà terminée, aucune modification ».
```

### t2 — Audit du dépôt réel et du contrat de `act`
- **id** : t2
- **Qui** : worker
- **Périmètre** : Audit en lecture seule du dépôt `/media/marcs/Linux_Apps/CloakBrowser-Agent/`, du wrapper MCP `/media/marcs/Linux_Apps/Projets_AI/mcp/cloak-agent/run-cloak-mcp.sh` et de la configuration `Opencodev2/config/opencode/opencode.json`. Examiner au minimum `cloak_agent/mcp_server.py`, `cloak_agent/agent.py`, `cloak_agent/browser.py`, `cloak_agent/model.py`, `cloak_agent/questions.py` et `tests/test_model.py`. Comparer l’état réel au dump fourni. Le dump contient déjà `act` dans `mcp_server.py` et `step` dans `agent.py` : confirmer si c’est toujours vrai dans le dépôt, identifier les écarts et les lacunes de test. Ne pas supposer l’existence de `server.py`, `tools.py`, de primitives dédiées supplémentaires ou d’un schéma Pydantic distinct.
- **Statut** : en attente
- **Prompt** :

```text
Tu travailles dans /media/marcs/Linux_Apps/CloakBrowser-Agent/. Fais un audit STRICTEMENT EN LECTURE SEULE de l’implémentation MCP `act`.

Lis les sources réelles, pas seulement le dump : cloak_agent/mcp_server.py, agent.py, browser.py, model.py, questions.py et tests/test_model.py. Vérifie aussi le wrapper /media/marcs/Linux_Apps/Projets_AI/mcp/cloak-agent/run-cloak-mcp.sh et l’entrée cloak-agent de /media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/opencode.json.

Le dump fourni contient déjà un serveur MCP basé sur MCPServer, avec les outils browse, snapshot, act et close_tab. `mcp_server.py` délègue l’action manuelle à `agent.step`; `browser.py` fournit `Session.act`, la vérification de fraîcheur et le traitement des pages obsolètes. Confirme ces points dans le dépôt réel avant toute conclusion.

Vérifie le contrat existant de act : un seul mode parmi (op + target) ou instruction; cibles issues du snapshot courant uniquement; TYPE_TEXT exige la valeur explicite dans le mode cible; instruction qui conduit à DONE/BLOCKED n’exécute aucune action; page obsolète renvoie stale sans retry; un snapshot est requis pour le mode op + target; les secrets restent masqués dans entrées/sorties, traces et erreurs; tab inconnue/occupée/fermée gérée sans fuite de données; contenu du site clairement marqué comme non fiable.

N’édite aucun fichier. Ne propose pas de nouvelle architecture et n’ajoute pas de primitives browser.py déjà couvertes par Session.act. Retourne : (1) état réel et chemins exacts, (2) API observée, (3) fonctions et garde-fous déjà présents, (4) tests existants pertinents, (5) lacunes prouvées classées bloquantes/majeures/mineures, (6) commandes de validation adaptées au dépôt. Si le dump est obsolète, indique précisément les différences. Ne déduis pas qu’un test a réussi sans l’avoir exécuté.
```

### t3 — Correction minimale des lacunes confirmées
- **id** : t3
- **Qui** : worker
- **Périmètre** : À partir du rapport t2 uniquement, corriger les lacunes fonctionnelles démontrées dans l’implémentation de `act`. Modifier le minimum de fichiers requis, normalement `cloak_agent/mcp_server.py` et/ou `cloak_agent/agent.py`; toucher `browser.py` seulement si l’audit prouve un défaut bas niveau non couvert. Si le dépôt possède déjà le contrat complet, ne change pas le code et déclare explicitement `NO_CODE_CHANGE_REQUIRED`. Ne pas créer de `server.py`, `tools.py` ni de primitives redondantes. Ne pas élargir le périmètre à `browse`, au modèle de décision ou à la configuration sans nécessité démontrée.
- **Statut** : en attente
- **Prompt** :

```text
Implémente uniquement les lacunes démontrées par le rapport t2 dans le dépôt /media/marcs/Linux_Apps/CloakBrowser-Agent/. Lis le code et les tests actuels avant de modifier quoi que ce soit.

La cible est l’outil MCP `act`, qui permet une action manuelle unique dans un onglet existant. Préserve le contrat public existant autant que possible : tab_id obligatoire; mode direct avec op et target issus du snapshot courant, ou mode instruction en langage naturel, jamais les deux à la fois; text fourni explicitement pour TYPE_TEXT en mode direct; retour d’un nouvel état après action; aucun retry automatique après StalePage; mode instruction choisi comme DONE/BLOCKED n’exécute rien.

Préserve les garde-fous existants : résolution de cibles à partir des seules actions observées, rejet d’un snapshot absent ou obsolète, traitement des onglets inconnus/occupés/fermés, redaction des secrets sur toutes les réponses, absence d’invention de mots de passe, et frontière explicite autour du contenu non fiable des pages. N’introduis aucun sélecteur libre permettant de contourner la résolution par snapshot.

N’ajoute pas de click_element/type_text_element/scroll_page/wait_for_selector/get_element_table si les fonctions existantes couvrent déjà ces opérations. N’invente pas les fichiers server.py ou tools.py : utilise l’architecture réellement auditée. Ne change pas les outils browse/snapshot/close_tab, la configuration OpenCode ou les variables d’environnement sauf si une lacune prouvée l’exige.

Si tout est déjà implémenté correctement, ne fais aucun changement et termine par `NO_CODE_CHANGE_REQUIRED` avec les preuves relevées. Sinon, livre un diff unifié minimal, liste chaque fichier modifié, justifie chaque modification en une phrase et donne les tests précis à exécuter. Ne prétends pas que les tests passent si tu ne les as pas lancés.
```

### t4 — Tests unitaires du contrat MCP `act`
- **id** : t4
- **Qui** : worker
- **Périmètre** : Auditer la couverture actuelle, puis ajouter uniquement les tests manquants, dans `tests/test_model.py` ou un fichier ciblé tel que `tests/test_mcp_act.py` selon les conventions réelles du dépôt. Couvrir les modes d’appel, cibles, erreurs, fraîcheur, absence de retry et redaction des secrets. Tester l’enveloppe MCP de `act` sans navigateur réel quand des mocks suffisent. Ne pas dupliquer les tests déjà présents.
- **Statut** : en attente
- **Prompt** :

```text
Dans /media/marcs/Linux_Apps/CloakBrowser-Agent/, complète la couverture de tests de l’outil MCP `act` après avoir lu les tests existants. N’écris aucun test déjà couvert à l’identique : tests/test_model.py contient déjà des tests de résolution de cibles, de `agent.step`, des secrets, des onglets et du formatage, donc vérifie la couverture réelle avant de choisir les ajouts.

Couvre toutes les lacunes prouvées du contrat, en priorité :
- un seul mode d’appel : op + target OU instruction;
- rejet d’un snapshot manquant, d’une opération inconnue et d’une cible qui n’appartient pas au snapshot courant;
- TYPE_TEXT sans text en mode direct ne modifie pas la page;
- une action sur page devenue obsolète retourne stale, n’exécute rien et ne retente pas;
- instruction aboutissant à DONE/BLOCKED n’exécute rien;
- secrets masqués dans le texte de confirmation, le snapshot, les erreurs et les entrées du modèle;
- onglet inconnu/occupé/fermé et erreur d’action traités sans fuite de secret ni état mensonger;
- mise à jour correcte de l’état conservé après une action réussie.

Utilise les conventions de test et de mock du dépôt. N’ajoute pas une nouvelle couche d’abstraction pour faciliter les tests. N’utilise pas de compte réel, de site tiers ou de soumission de formulaire. Exécute les tests ciblés puis la suite existante avec l’interpréteur/environnement du projet. Retourne les fichiers modifiés, les commandes exactes et les résultats réels PASS/FAIL; si une dépendance ou un service manque, indique-le comme blocage et n’invente pas un résultat.
```

### t5 — Revue indépendante sécurité et régression
- **id** : t5
- **Qui** : reviewer
- **Périmètre** : Revue en lecture seule du diff de t3 et des tests de t4, en les comparant aux sources complètes et aux garde-fous du dump. Vérifier notamment injection d’instructions par le contenu web, redaction des secrets, actions sur cible obsolète, faux succès, comportement de `tab.view` et limites de responsabilité entre MCPServer, `agent.step` et `Session.act`. Ne pas réécrire le code.
- **Statut** : en attente
- **Prompt** :

```text
Effectue une revue indépendante EN LECTURE SEULE du diff produit par t3 et des tests produits par t4 dans /media/marcs/Linux_Apps/CloakBrowser-Agent/. Vérifie les sources complètes et le dump d’architecture. N’applique aucune correction.

Cherche les régressions concrètes, en particulier :
1. action dirigée vers un index périmé ou une action qui n’était pas dans le dernier snapshot;
2. retry automatique après StalePage, ou action exécutée alors que le mode instruction a choisi DONE/BLOCKED;
3. confusion entre op + target et instruction, ou usage incorrect de text;
4. secret divulgué dans les modèles, le snapshot, les logs, une exception ou un retour MCP;
5. contenu de page non fiable interprété comme une instruction système;
6. course entre appels sur un même onglet, état tab.view obsolète, ou statut `done` annoncé alors qu’aucune action n’a été exécutée;
7. tests qui passent par un mock mais ne vérifient pas réellement le contrat exposé par l’outil MCP.

Pour chaque constat, donne sévérité (bloquant/majeur/mineur), chemin et lignes concernés, scénario reproductible et test qui le démontre ou devrait le démontrer. Sépare les défauts certains des risques non démontrés. Si aucun défaut n’est trouvé, dis explicitement « aucun défaut confirmé » et cite les vérifications réalisées. Ne réclame pas de refonte ou de changements hors périmètre.
```

### t6 — Correction des constats validés
- **id** : t6
- **Qui** : worker
- **Périmètre** : Corriger uniquement les constats bloquants ou majeurs confirmés par t5, puis ajouter/adapter un test de régression pour chacun. Les points mineurs peuvent être laissés documentés s’ils ne compromettent ni le contrat ni la sécurité. Aucun élargissement du périmètre.
- **Statut** : terminée
- **Modèle** : llama-local/ornith1.5-9b
- **Preuve** : t5 = « aucun défaut confirmé » → NO_FIX_REQUIRED

```text
Traite le rapport de revue t5 dans /media/marcs/Linux_Apps/CloakBrowser-Agent/. Corrige uniquement les problèmes bloquants ou majeurs confirmés par une preuve reproductible. Pour chaque correction, ajoute un test de régression qui échoue avant la correction et passe après si cette comparaison est réalisable dans l’environnement actuel.

Ne corrige pas les suggestions stylistiques ni les risques hypothétiques sans reproduction. Préserve le contrat public et tous les garde-fous existants. Ne touche pas aux fichiers sans lien avec un constat validé. Exécute les tests concernés et la suite complète, puis fournis un diff minimal, la correspondance constat → correction → test et les résultats réels. Si t5 ne relève aucun défaut confirmé, ne modifie rien et retourne `NO_FIX_REQUIRED`.
```

### t7 — Validation E2E locale du flux MCP
- **id** : t7
- **Qui** : worker
- **Périmètre** : Vérifier le flux réel `browse → snapshot → act → snapshot → close_tab` sur une fixture locale déterministe et non destructive. Utiliser le client/transport MCP et la configuration réellement présents. Vérifier au minimum saisie, clic, scroll/wait et sélection si la fixture et l’API la prennent en charge. Ne pas créer de compte Proton ni d’autre compte réel; ne pas soumettre de formulaire, effectuer de paiement ou déclencher une mutation sur un site tiers. Si l’environnement nécessaire manque, déclarer le blocage précis.
- **Statut** : en attente
- **Prompt** :

```text
Valide le flux de bout en bout de `cloak-agent` dans /media/marcs/Linux_Apps/CloakBrowser-Agent/ en utilisant le transport/client MCP réellement configuré dans le projet. Examine le wrapper run-cloak-mcp.sh et l’entrée `cloak-agent` dans opencode.json avant de lancer le serveur. Ne modifie pas les secrets ou profils utilisateur et n’affiche aucune valeur secrète dans le rapport.

Utilise une page HTML de test servie localement ou une fixture locale déterministe. Le scénario doit ouvrir une page avec un champ de texte, un bouton observable qui change l’état, un contrôle sélectionnable et une page assez longue pour tester le défilement. Exécute le flux `browse` pour obtenir un tab_id, `snapshot` pour obtenir les indices actuels, `act` pour remplir le champ, reprendre un snapshot après chaque changement d’état, cliquer sur le bouton, effectuer un scroll ou WAIT, tester SELECT si pris en charge par la fixture, puis `close_tab`.

Vérifie que les changements sont visibles après chaque action, que les indices utilisés viennent du snapshot courant et que l’onglet est fermé à la fin. Ne clique jamais sur un bouton de soumission réel. Ne crée aucun compte Proton, ne contacte pas de site tiers et n’effectue aucune action irréversible ou transactionnelle.

N’invente pas de résultat E2E si le navigateur, la licence, les dépendances ou le transport ne sont pas disponibles. En cas de blocage, rapporte la commande lancée, l’erreur pertinente expurgée de tout secret et la condition exacte nécessaire pour reprendre. Sinon, fournis les étapes réellement exécutées, les résultats observés et les commandes de reproduction.
```

### t8 — Vérification finale de la livraison
- **id** : t8
- **Qui** : reviewer
- **Périmètre** : Contrôler que le diff final est limité au périmètre, que les tests ciblés et la suite passent, que le test E2E est effectivement exécuté ou explicitement bloqué, qu’aucun secret ou changement utilisateur n’est inclus et que la documentation/configuration n’a été touchée qu’en cas de nécessité. Fournir un verdict GO/NO-GO sans modifier les fichiers.
- **Statut** : en attente
- **Prompt** :

```text
Fais le contrôle final indépendant avant merge dans /media/marcs/Linux_Apps/CloakBrowser-Agent/. Ne modifie aucun fichier.

Vérifie le diff complet, l’état Git, les tests ciblés, la suite complète et le rapport E2E de t7. Confirme que les fichiers modifiés sont nécessaires, que les tests s’appuient sur le contrat réel et qu’aucune donnée secrète, configuration personnelle, changement préexistant de l’utilisateur ou fichier généré non pertinent n’est inclus.

Critères GO obligatoires : API act cohérente avec le serveur MCP existant; cibles issues du snapshot courant; aucun retry sur stale; aucune exécution dans le cas DONE/BLOCKED; secrets redacted; tests passants; E2E local réussi ou blocage environnemental clairement établi sans le présenter comme un succès. Toute régression de sécurité, test rouge ou diff sans justification donne NO-GO.

Retourne uniquement un verdict GO ou NO-GO, suivi des preuves, des commandes et résultats exacts, et de la liste des risques restant ouverts. Ne dis pas qu’un test a passé s’il n’a pas été exécuté.
```

### t9 — Merge Git et livraison
- **id** : t9
- **Qui** : merger
- **Périmètre** : Uniquement après GO de t8, vérifier la branche prévue `orch/cloak-act-tool`, l’état de l’arbre et l’absence de modifications utilisateur étrangères. Committer et pousser uniquement les modifications validées. Ne jamais écraser des changements préexistants ni forcer le push. Si aucune modification n’est requise, ne crée pas de commit vide.
- **Statut** : en attente
- **Prompt** :

```text
Tu es le merger. N’interviens qu’après un verdict GO explicite de t8 dans /media/marcs/Linux_Apps/CloakBrowser-Agent/.

Vérifie la branche actuelle, le statut Git, le diff final et les résultats attachés. Utilise la branche `orch/cloak-act-tool` uniquement si elle existe et correspond au flux du dépôt; ne change pas de branche en écrasant du travail. N’ajoute que les fichiers nécessaires et validés. Préserve toutes les modifications préexistantes de l’utilisateur, n’utilise jamais `git reset --hard`, `git checkout --` destructif ou `git push --force`.

Si les tests requis sont en échec, si le verdict GO manque ou si le diff contient des changements non validés, bloque le merge et rapporte la cause. Si aucun changement de code n’a été nécessaire, ne fais pas de commit vide : retourne `NO_COMMIT_REQUIRED` avec l’état Git. Sinon, crée un commit avec un message descriptif, pousse sans force selon la convention existante, puis retourne le SHA du commit, la branche, le statut du push et l’URL de PR uniquement si elle existe réellement. N’invente ni SHA ni URL.
```