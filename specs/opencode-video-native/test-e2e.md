# Test end-to-end — lecture vidéo native OpenCode V2

- Carte : `t4` (session `ses_df2d80b4bffer6ZIJyuBIxt14W`)
- Date : 2026-11-24, 15:33 → 16:10 CET
- Binaire testé : `/media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode`
  (`opencode v0.0.0-dev-202611241422`, 267 393 200 octets,
  sha256 `918db9f12600f93dbbd7219ddb5bf57987ab0e63a494e4679dd80c4c4da479a6`) —
  celui posé par `t3`, identique à la sortie du build patché (preuve `t3r`).
- Lanceur : `/media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode-v2 run` → `opencode run --standalone`
  (serveur privé sur le **nouveau** binaire ; le service partagé n'est jamais touché).
- Toutes les commandes ci-dessous ont été exécutées telles quelles ; les extraits de réponse sont
  copiés du terminal.

## 0. Matériel de test

Vidéo 5 s, 640×360, 25 fps : fond rouge `0xB71C1C`, bande verte `0x2E7D32` à gauche (160 px),
bandeau sombre, titre cyan `COMPTEUR`, compteur blanc `SECONDE 1`…`SECONDE 5`.

```bash
mkdir -p /tmp/opencode && cd /tmp/opencode
ffmpeg -y -f lavfi -i "color=c=0xB71C1C:s=640x360:d=5:r=25" \
  -vf "drawbox=x=0:y=0:w=160:h=360:color=0x2E7D32@1:t=fill,\
drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='SECONDE %{eif\:t+1\:d}':fontcolor=white:fontsize=64:x=(w-text_w)/2+80:y=(h-text_h)/2:box=1:boxcolor=black@0.5:boxborderw=20,\
drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='COMPTEUR':fontcolor=0x00E5FF:fontsize=40:x=(w-text_w)/2+80:y=40" \
  -c:v libx264 -pix_fmt yuv420p -movflags +faststart /tmp/opencode/test-video.mp4
```

```
codec_name=h264  width=640  height=360  r_frame_rate=25/1  duration=5.000000  size=15060
```

Pièces jointes de non-régression, générées au même endroit :

- `/tmp/opencode/test-image.png` — une frame extraite de la vidéo (`ffmpeg -ss 2.5 -frames:v 1`),
  donc contenu connu : bande verte, fond rouge, `COMPTEUR` cyan, `SECONDE 3` blanc.
- `/tmp/opencode/test-doc.pdf` — PDF 1 page, Helvetica 18 pt, deux lignes :
  `PDF DE TEST` / `Le code secret est BANANE BLEUE 42` (vérifié par `pdftotext`).

Choix du modèle vidéo : `./opencode-v2 models` (catalogue effectif) ne contient **ni**
`opencode-go/crouton-alpha-free` **ni** `opencode-go/space-bunny` (les deux noms du brief) ;
`opencode-go/qwen3.7-plus` est présent et déclare `video` dans ses modalités d'entrée
(models.dev, entrée `opencode-go/qwen3.7-plus` : `input: ["text","image","video"]`). C'est donc
`qwen3.7-plus` qui sert de modèle vidéo, conformément au repli nommé par la carte.

## 1. Cas A — vidéo + modèle vidéo, attachement CLI (`--file`)

```bash
cd /media/marcs/Linux_Apps/Projets_AI
./Opencodev2/opencode-v2 run --model opencode-go/qwen3.7-plus \
  --file /tmp/opencode/test-video.mp4 --archived \
  "Décris précisément ce que tu vois dans la vidéo attachée : couleurs, texte, chiffres."
```

Le CLI `run` lit le fichier et l'envoie en data URL (`data:video/mp4;base64,…`), forme décrite
en spec.md §1.f. Réponse (extrait intégral du corps utile) :

```
**Composition visuelle :**
- L'image est divisée en deux zones de couleur : une bande **verte** (vert foncé/sombre) sur le côté
  gauche, occupant environ 25 % de la largeur, et le reste de l'écran en **rouge** (rouge assez
  vif/écarlate).
**Texte et chiffres :**
1. En haut, centré sur la partie rouge, le mot **"COMPTEUR"** est écrit en lettres majuscules de
   couleur **cyan (bleu clair)**.
2. Au centre de la partie rouge, une **bande rectangulaire sombre** … contient un texte en lettres
   majuscules **blanches**.
3. Ce texte central affiche un **compteur de secondes** qui s'incrémente d'une seconde à chaque frame :
   - Frame 0,2 s → **"SECONDE 1"**   - Frame 1,2 s → **"SECONDE 2"**   - Frame 2,2 s → **"SECONDE 3"**
   - Frame 3,2 s → **"SECONDE 4"**   - Frame 4,2 s → **"SECONDE 5"**
```

**Résultat : PASS** — couleurs (bande verte / fond rouge / titre cyan / bandeau sombre), textes et
les cinq valeurs du compteur sont exacts. La vidéo est bien transmise au modèle, pas seulement
admise.

## 2. Cas B — vidéo + modèle vidéo, attachement URI `file:///` (chemin serveur)

Ce cas exerce la branche **`readFileAttachment`** (stat + `attachmentCeiling`, cf. spec.md §2.4) que
le data URL du CLI contourne : c'est exactement le code touché par `t2c`. Il est piloté par l'API v2
du binaire patché (`serve --port 4199`, arrêté après le test) :

```bash
cd /media/marcs/Linux_Apps/Projets_AI
(nohup ./Opencodev2/opencode-v2 serve --port 4199 > /tmp/opencode/serve-t4.log 2>&1 &)

./Opencodev2/opencode-v2 api --server http://127.0.0.1:4199 POST /api/session \
  -d '{"title":"t4 video file-uri","model":{"providerID":"opencode-go","id":"qwen3.7-plus"}}'
# → session ses_df1fb3c5fffee79DJ7rgoS5KEQ

./Opencodev2/opencode-v2 api --server http://127.0.0.1:4199 \
  POST /api/session/ses_df1fb3c5fffee79DJ7rgoS5KEQ/prompt \
  -d '{"text":"Décris précisément ce que tu vois dans la vidéo attachée (fichier file:///tmp/opencode/test-video.mp4) : couleurs, texte, chiffres.","files":[{"uri":"file:///tmp/opencode/test-video.mp4","name":"test-video.mp4"}]}'
```

L'écho de la requête montre l'attachement matérialisé côté serveur : `"mime":"video/mp4"`,
`"source":{"type":"uri","uri":"file:///tmp/opencode/test-video.mp4"}`, `name:"test-video.mp4"`, et
un `data` base64 commençant par l'en-tête MP4 `AAAAIGZ0eXBpc29t` (`ftyp isom`) — le serveur a bien
lu les octets du `file:///`, l'admission est passée (le cap vidéo n'est pas éprouvé : fichier de 15 060 o, bien sous les 20 MiB et sous `media.video.max_bytes`). Réponse (session
`ses_df1fb3c5fffee79DJ7rgoS5KEQ`, statut `idle / succeeded`) :

```
- **Fond** : divisé en deux zones — une bande verticale **verte** sur le côté gauche (~20 % de la
  largeur), le reste en **rouge vif**.
- **Texte « COMPTEUR »** : en **cyan** (bleu clair vif).
- **Bandeau central** : un rectangle horizontal **bordeaux/marron foncé** …
| 0.2 s | SECONDE **1** |   1.2 s | SECONDE **2** |   2.2 s | SECONDE **3** |
| 3.2 s | SECONDE **4** |   4.2 s | SECONDE **5** |
- La bande verte à gauche ne contient aucun texte.
```

**Résultat : PASS** — format d'attachement URI `file:///` accepté et décrit correctement (hypothèse de mécanisme, non mesurée : le modèle a probablement lu des frames inlinées, cohérent avec le transport `video_url` retenu en spec.md §0).

## 3. Cas C — modèle sans entrée vidéo : note courte de fallback

Modèle visé par la carte : `llama-local/ornith1.5-9b`. **Tenté d'abord, non utilisable** :

```
Model load failed: Ornith1.5-9b exited with code null (signal SIGSEGV):
failed to initialize ROCm: no ROCm-capable device is detected
(warning: no usable GPU found, --gpu-layers option will be ignored ; 11 tentatives)
```

C'est une panne de chargement du proxy local llama.cpp (ROCm/GPU), antérieure et étrangère au patch
vidéo — aucun octet de la pièce jointe n'est en cause. Substitut retenu, même chemin de code de
fallback : `opencode-go/deepseek-v4.1-flash` (entrée déclarée `["text","image"]`, donc **sans**
`video`).

```bash
./Opencodev2/opencode-v2 run --model opencode-go/deepseek-v4.1-flash \
  --file /tmp/opencode/test-video.mp4 --archived \
  "Que contient la vidéo attachée ? Si tu ne peux pas la lire, recopie exactement le message d'erreur que tu as reçu, mot pour mot."
```

```
Je ne peux pas la lire : ce modèle n'accepte pas les vidéos en entrée.

Message d'erreur exact reçu :

ERROR: Cannot read "test-video.mp4" (this model does not support video input).

Si tu veux quand même savoir ce qu'elle contient, je peux la faire analyser autrement : extraction
d'images clés via `ffmpeg` puis lecture des frames (celles-ci passent, ce sont des images) …
```

**Résultat : PASS** — la note courte reproduit celle de `SessionModelRequest.unsupportedMedia` (spec.md §1.d) sur le fragment `ERROR: Cannot read "test-video.mp4" (this model does not support video input).` — l'extrait du modèle s'arrête là et omet la suite « Inform the user. » présente dans la source ; le nom du fichier est cité, le run se termine proprement (aucun crash, aucune erreur d'attachement), et le modèle n'a jamais reçu les octets vidéo.

## 4. Cas D — second provider `commandcode` : non testé (non bloquant)

`CMD_API_KEY` est **absente** de l'environnement et ne m'a pas été fournie. Le magasin de
credentials du binaire installé ne contient qu'un provider :

```bash
python3 -c "import json;print(sorted(json.load(open('/media/marcs/Linux_Apps/Projets_AI/Opencodev2/data/opencode/auth.json')).keys()))"
# ['opencode-go']
```

Le cas `commandcode/google/gemini-3.6-flash` est donc **implémenté, non vérifié** : il emprunte le
même unique chemin de code que le cas A/B (`@ai-sdk/openai-compatible` → protocole
`OpenAIChat` → partie `{"type":"video_url"}`), la seule différence étant l'endpoint
`https://api.commandcode.ai/provider/v1` (spec.md §0). Aucune conclusion serveur n'est tirée pour ce
provider.

## 5. Cas E — non-régression PNG

```bash
./Opencodev2/opencode-v2 run --model opencode-go/gemini-4-argon \
  --file /tmp/opencode/test-image.png --archived \
  "Décris précisément l'image attachée : couleurs, textes, chiffres."
```

```
* **Bande gauche (environ 1/4 de la largeur) :** Un rectangle vertical de couleur **vert foncé / vert forêt**.
* **Zone droite (environ 3/4 de la largeur) :** Un grand fond de couleur **rouge vif / rouge brique**.
* **Encadré central …** **marron très foncé / bordeaux sombre**
1. … Texte : **COMPTEUR** … Couleur : **Bleu cyan / turquoise vif**.
2. … Texte et chiffre : **SECONDE 3** … Couleur : **Blanc**.
```

**Résultat : PASS** — image traitée normalement, réponse exacte, aucune erreur.

## 6. Cas F — non-régression PDF

```bash
./Opencodev2/opencode-v2 run --model opencode-go/gemini-4-argon \
  --file /tmp/opencode/test-doc.pdf --archived \
  "Que contient le PDF attaché ? Donne le code secret exact."
```

```
Le PDF attaché contient une seule page avec le texte suivant :
> PDF DE TEST
> Le code secret est BANANE BLEUE 42
Le code secret exact est donc : `BANANE BLEUE 42`
```

**Résultat : PASS** — PDF traité normalement (branche `application/pdf` intacte), réponse exacte,
aucune erreur.

## 7. Vérification TUI manuelle — optionnelle, NON EXÉCUTÉE

Le DONE de la carte ne l'exige pas (la preuve est le `run`). Elle consisterait à ouvrir
`./Opencodev2/opencode-v2`, coller un `.mp4` dans le prompt (mention `@`) et vérifier l'affichage de
la pièce jointe. **Non exécutée** : interactive, et les trois chemins qu'elle couvrirait (admission,
transmission, fallback) sont déjà prouvés ci-dessus. À noter pour elle : la table d'extensions du TUI
ne connaît pas `.mp4` (spec.md §1.f, §2.5), donc la pièce jointe arriverait en
`application/octet-stream` puis serait re-détectée par les octets côté serveur — comportement
attendu, non mesuré ici.

## 8. Limites qui restent vraies après ce test

1. **Client CLI** : `run --file` refuse au-delà de 10 MiB (`ATTACH_FILE_MAX_BYTES`) ; la vidéo de test
   fait 15 Ko, la limite n'est donc pas éprouvée ici (spec.md §3.3.1).
2. **Budget inline** : une vidéo n'est jamais retirée d'un historique long par `boundImages`
   (spec.md §3.3.2).
3. **Tokens** : `estimateMedia` compte 0 pour la vidéo, la compaction sous-estime (spec.md §3.3.3).
4. **Formats** : seuls mp4/webm/quicktime sont admis ; avi/mkv restent écartés (spec.md §3.3.4).
5. **Second provider** : `commandcode` non vérifié faute de credential (cas D).

## VERDICT: PASS

Vidéo lue et décrite correctement par un modèle à entrée vidéo, par les **deux** formes
d'attachement (data URL du CLI et URI `file:///` du serveur) ; note courte de fallback exacte et
sans crash pour un modèle sans entrée vidéo ; PNG et PDF inchangés. Réserve unique, documentée et
non bloquante : `commandcode` implémenté, non vérifié (aucune credential disponible).
