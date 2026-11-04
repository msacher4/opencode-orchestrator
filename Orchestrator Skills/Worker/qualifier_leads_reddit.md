---
name: qualifier_leads_reddit
description: >
  Savoir-faire pour trier un flux Reddit : qui est un vrai opérateur Shopify/DTC, qui est un
  débutant, une agence ou un compte mort, et ce qui rend un lead exploitable pour une réponse
  publique. Concerne le jugement, pas la collecte ni la rédaction.
---

# Juger un lead Reddit

Ce skill porte le **jugement**, pas la procédure. La carte dit quelles actions faire ; ici on dit
comment trancher, et pourquoi.

## 1. La grille, dans l'ordre

1. **Fraîcheur** : post de moins de 24 h obligatoire. Moins de 4 h = priorité, jamais un filtre.
2. **Débutant = exclusion ferme** : « je démarre », premier produit, zéro vente, boutique pas
   lancée. Il n'a pas de budget, et lui proposer du CRO est hors sujet.
3. **Compte mort / gelé / banni = exclusion ferme.** Vendeur d'outil ou de SaaS en promo =
   exclusion ferme : il vend, il n'achète pas.
4. **Rôle** : owner ou opérateur qui décide. Agence, media buyer freelance, consultant = hors
   cible — il ne paie pas pour ce qu'il fait déjà.
5. **Plateforme** : on n'écarte **jamais** sur la plateforme. Shopify cité = bonus, pas condition.
6. **Volume et douleur ne servent pas à exclure** : ils **classent**. CA ≥ 15-20 k$/mois,
   ≥ 20 commandes/jour, budget ≥ 3 k$/mois = fit élevé.
7. **Douleur hors sujet** = exclusion : logistique, douane/TVA, RH, juridique, compta, devis,
   fournisseurs, simple question d'app.
8. **Un post sans chiffre exploitable = exclusion.** Pas de nombre, pas de pourcentage, pas de
   montant, pas de volume : la réponse publique ne pourra rien ancrer, le lead est mort d'avance.

## 2. Ce qui fait un lead exploitable

Un lead exploitable, c'est un post qui contient **un chiffre** (montant, pourcentage, volume,
durée) et **une douleur** que ce chiffre décrit. Sans les deux, la réponse sera creuse : soit elle
n'a rien à citer, soit elle cite un chiffre qui ne veut rien dire.

Le chiffre doit être **dans le post**, tel qu'il est écrit. Pas « environ 300 », pas « quelques
milliers » : `$300`, `70%`, `5.5`, `122 ATC`.

## 3. Les motifs d'exclusion sont fermés

La grille en donne huit. **Tu n'en inventes aucun autre.** Si tu hésites entre deux catégories,
tu retiens le lead et tu écris « à confirmer » dans ton contrat.

**Promoteur d'un autre produit, créateur de contenu, vendeur de formation = exclusion ferme.**
Ce n'est pas un opérateur DTC : il vend, il n'achète pas.

C'est l'erreur la plus fréquente des workers : sur-appliquer une règle. Le 03/11, un worker a
écarté un subreddit explicitement autorisé en invoquant une « porte du sub » qui n'existait pas,
et il a fallu re-découper l'étape. Un motif d'exclusion qui n'est pas dans la liste est une
invention.

## 4. Ce que le fichier de sortie doit contenir, et pourquoi

- `chiffres` : **tous** les nombres du post, recopiés tels quels. C'est la réserve dans laquelle
  la rédaction puisera — si elle est vide, la rédaction est condamnée.
- `existing_comments` : les commentaires déjà là. Ils servent à ne pas répéter ce qui est déjà dit.
- `angles_deja_pris` : ce qui a **déjà été dit**, en clair, en 3 à 6 angles courts. C'est le seul
  champ qui demande ton jugement : le reste est mécanique.
- `commentaires_lus` : combien tu en as réellement lus.

Un `angles_deja_pris` vide sur un post qui a 15 commentaires est un mensonge : ça veut dire que la
rédaction va répéter un angle déjà pris, et le reviewer le verra.

## 5. Le plafond est un garde-fou, pas un objectif

12 leads maximum, pour tenir dans un message Telegram et dans un temps de rédaction raisonnable.
**Pas de plancher** : si 3 posts passent la grille, on en livre 3. Forcer un lead pour « faire du
volume » est exactement ce qui produit des réponses creuses.

## 6. Les pièges vus dans les runs

- **02/11** : 12 retenus, 42 écartés — dont 21 en « autre ». Quand « autre » domine, c'est que la
  grille est sur-appliquée.
- **04/11** : 10 retenus, 11 écartés dont 6 agence/SaaS/B2B/lead-gen. C'est sain.
- **02/11** : 3 leads retenus n'avaient **aucun chiffre** dans leur post. La rédaction ne pouvait
  pas les réussir : ils sont sortis en `blocked` et une étape de rattrapage a été nécessaire.
- **03/11** : un sub autorisé écarté par un motif inventé.
- **04/11** : le fichier de sortie a été écrit avec les clés du fichier brut
  (`id/subreddit/title/author/top_comments`), et l'étape suivante a dû le refabriquer à la main.
- **04/11** : 6 posts retenus sur 52, et la review en a trouvé **4 de plus conformes** — le worker
  écartait trop. **Si tu écartes plus de 8 posts sur 10, relis la grille avant de rendre — c'est le
  signe que tu appliques les critères trop durement.**

## 7. La checklist avant de rendre

- [ ] chaque post retenu a moins de 24 h
- [ ] aucun débutant, aucun compte mort, aucune agence retenue
- [ ] personne n'a été écarté à cause de sa plateforme
- [ ] aucun motif d'exclusion hors des huit
- [ ] chaque post retenu contient au moins un chiffre, et `chiffres` les liste
- [ ] un seul post par handle, et aucun handle déjà présent dans `Leads`
- [ ] **avant de retenir un candidat, recoupe son handle avec la liste `Leads` (`tools/worker/sheet_list.py --sheet Leads`) ; s'il y est déjà, écarte-le et compte-le en `IGNORÉS`**
- [ ] `angles_deja_pris` rempli pour chaque post qui a des commentaires
- [ ] le fichier a les clés attendues, pas celles du fichier brut
