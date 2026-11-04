---
name: livrer_dans_leads
description: >
  Savoir-faire pour contrôler des réponses avant livraison et les faire entrer dans l'onglet
  Leads sans rien jeter : les 12 points de contrôle, les colonnes interdites, la dédup et le
  piège de l'onglet par défaut. Concerne le contrôle et la livraison, pas l'écriture.
---

# Contrôler puis livrer dans `Leads`

Ce skill porte le **savoir du contrôle et de la livraison**. La carte dit quoi appeler et où
écrire ; ici on dit ce qui fait qu'une ligne est bonne, et ce qu'on ne touche jamais.

## 1. Le principe : on ne jette jamais un lead

Un lead écarté, c'est du travail perdu et une ligne en moins dans la sheet. **Une réponse fautive
se corrige** — on sait écrire — puis on livre. Jeter est un aveu : soit le contrôle est trop
strict, soit la rédaction était mauvaise, et dans les deux cas la sortie n'est pas de supprimer.

Le seul cas où un lead ne se livre pas, c'est un `blocked` assumé de la rédaction : post sans
chiffre, réponse impossible. Et il doit être **écrit** dans le contrat, pas silencieux.

## 2. Les 12 points de contrôle

Dans cet ordre, pour chaque réponse :

1. anglais uniquement
2. 280 caractères ou moins (recomptés, pas lus)
3. exactement un `?`
4. un chiffre qui vient du post — brut, ou déduit exactement de deux chiffres du post. **Aucun
   chiffre inventé, aucun chiffre qui contredit le post**
5. aucun lien, prix, calendrier, hashtag, markdown
6. zéro ou un emoji
7. ne commence pas par `Hey` / `Hi` / `Hello`
8. aucun pitch (`I help`, `we`, `DM me`, `happy to share`, `book a call`)
9. **aucune félicitation creuse** : `great post`, `impressive`, `congrats`, `nice work`,
   `well done`, `solid`
10. l'angle n'est pas dans `angles_deja_pris`, ni adjacent
11. aucun mot IA (`delve`, `leverage`, `unlock`, `game-changer`, `robust`, `comprehensive`,
    `streamline`, `it's not X, it's Y`)
12. zéro tiret cadratin, zéro point-virgule, apostrophes droites

**Le point 9 est celui qui a coûté un lead** : « impressive » n'était contrôlé nulle part avant la
livraison, et le 02/11 la seule sortie possible était de jeter la ligne.

**La porte — avant de livrer, recoupe chaque chiffre de chaque réponse avec le post :**
- **direct** : le chiffre est recopié tel quel du post → passe
- **déduit exactement** : opération exacte entre deux chiffres du post (ex. 20 − 2 = 18) → passe
- **introuvable** : le chiffre n'est ni dans le post ni déductible exactement → **bloque la livraison**

## 3. Les colonnes de Marc — interdites d'écriture

`Archive`, `Date d'envoie`, `Contacté`, `Method contact`, `Réponse?`, `Relance?`

Ce sont ses colonnes de suivi, il les remplit lui-même. Le script les protège ; toi aussi. Une
ligne livrée qui écrase une de ces colonnes est une perte de données, pas un détail.

## 4. Le piège de l'onglet par défaut

`gsheet_append.py` **sans `--sheet`** écrit dans le **premier onglet du classeur**, qui est
aujourd'hui `Email` — donc à côté, et sans erreur visible : le script répond `ok: true`. Vérifié le
04/11. L'onglet doit être nommé explicitement, à chaque appel, même quand « c'est évident ».

Et jamais `Visibility` : c'est l'onglet des posts à commenter, pas des leads.

## 5. La dédup

Le script déduplique sur `Name` + `Contact`. Une ligne déjà présente revient en `skipped` : c'est
**normal**, ça veut dire que le lead est déjà livré. On ne force jamais un `skipped` en réécrivant
la ligne, et on ne « corrige » pas un doublon en changeant le handle.

## 6. Ce que doit contenir une ligne

- `name` : `u/<handle>`
- `recap` : 90 caractères maximum, la boutique ou la niche + le chiffre ou la douleur exacte du
  post. **Aucun chiffre inventé.**
- `contact` : `Reddit: u/<handle>`
- `message` : la réponse publique, telle quelle, jamais un DM, jamais retouchée après contrôle
- `link` : l'URL exacte du post

## 7. Les comptes doivent tomber juste

`retenus − bloqués = lignes livrées = lignes annoncées au Telegram`.

Si les comptes ne tombent pas juste, tu t'arrêtes et tu le signales : c'est le signe qu'une ligne a
été perdue en route, et c'est exactement ce que le reviewer va chercher.

**Le contrat porte `AJOUTÉS` **et** `IGNORÉS`** (le 04/11, `IGNORÉS: 1` manquait et a provoqué un
faux décalage).

## 8. Les pièges vus dans les runs

- **02/11** : `AdhesivenessFull9250` jeté pour un « impressive » → 7 lignes livrées au lieu de 8.
- **02/11** : 12 retenus − 5 bloqués = 7 livrés ; les comptes tombaient juste, c'est ce qui a
  permis au reviewer de valider.
- **04/11** : `--list` sans `--sheet` a répondu `tab: Email` avec des en-têtes vides.
- **02/11** : un `recap` contenait un chiffre absent du post.

## 9. La checklist avant de rendre

- [ ] les 12 points passés sur chaque réponse
- [ ] les réponses fautives **corrigées**, aucune supprimée
- [ ] nombre de lignes = nombre de réponses `ok`
- [ ] l'onglet est nommé explicitement dans l'appel
- [ ] un seul appel pour tout le lot
- [ ] la sortie du script recopiée telle quelle dans le contrat (`ok`, `added`, `skipped`)
- [ ] aucune colonne de Marc écrite
- [ ] `recap` ≤ 90 caractères, sans chiffre inventé
- [ ] les comptes tombent juste
