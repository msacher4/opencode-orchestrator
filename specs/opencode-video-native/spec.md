# Spec — lecture vidéo native dans OpenCode V2

- Chantier : `opencode-video-native`, session `ses_df2d80b4bffer6ZIJyuBIxt14W`, carte `t1`
- Source de vérité du code : clone shallow `Opencodev2/opencode-src` (branche `dev`,
  `git log --oneline -1` = `678621a fix(app): make the worktree step's back control the search-field arrow (#66090)`,
  `package.json` version `2.4.1`)
- Cible : `packages/core`, `packages/schema`, `packages/cli` (aucun changement de `packages/ai`)
- Licence upstream MIT ; brouillon de PR seulement (carte `t5`)

## 0. Décision de transport (la question de la condition d'arrêt)

**Les deux transports visés peuvent porter les octets vidéo. Aucun `BLOQUÉ`.**

Chaîne établie par le code, pas par supposition :

1. `models.dev` déclare, pour les trois providers concernés, le paquet `@ai-sdk/openai-compatible` :

   | provider     | api (models.dev)                                    |
   | ------------ | --------------------------------------------------- |
   | `opencode`   | `https://opencode.ai/inference/openai/v1`           |
   | `opencode-go`| `https://opencode.ai/inference/go/openai/v1`        |
   | `commandcode`| `https://api.commandcode.ai/provider/v1`            |

   Il n'existe **pas** d'endpoint `https://opencode.ai/zen/v1` : la doc Console
   (<https://opencode.ai/docs/zen>) donne `https://opencode.ai/inference/openai/v1/chat/completions`
   pour les modèles de chat, `…/inference/systemone/v1/systemone` pour les modèles de décision, et
   `…/inference/go/openai/v1` pour `opencode-go` (models.dev). Le brief citait « zen » : c'est
   l'ancien nom de l'API, le chemin retenu est celui du tableau ci-dessus.

2. `packages/core/src/aisdk-native.ts` réécrit ce paquet vers le provider natif :

   ```ts
   "@ai-sdk/openai-compatible": "@opencode/ai/providers/openai-compatible",
   ```

   et `packages/ai/src/providers/openai-compatible.ts` expose cette route :

   ```ts
   export const routes = [OpenAICompatibleChat.route]
   ```

   donc protocole `OpenAIChat.protocol` (`packages/ai/src/protocols/openai-chat.ts`) pour les trois
   providers. La branche `SystemOne` de ce provider ne sert qu'aux modèles de décision.

3. Ce protocole sait déjà sérialiser une vidéo, en data URL inline :

   ```ts
   const lowerMedia = Effect.fnUntraced(function* (part: MediaPart) {
     // Chat Completions accepts PDFs, and no other documents, as inline `file` parts; file URLs are not supported.
     if (part.media.mediaType.toLowerCase() === "application/pdf")
       return {
         type: "file" as const,
         file: {
           filename: part.filename ?? "document.pdf",
           file_data: (yield* ProviderShared.requireInlineMedia("OpenAI Chat", part.media)).dataUrl,
         },
       }
     if (part.media.kind !== "image" && part.media.kind !== "video")
       return yield* ProviderShared.invalidRequest(`OpenAI Chat does not support media type ${part.media.mediaType}`)
     const url =
       ProviderShared.mediaUrl(part.media) ?? (yield* ProviderShared.requireInlineMedia("OpenAI Chat", part.media)).dataUrl
     if (part.media.kind === "video") return { type: "video_url" as const, video_url: { url } }
     return { type: "image_url" as const, image_url: { url } }
   })
   ```

   et le schéma de requête admet la partie :

   ```ts
   Schema.Struct({
     type: Schema.Literal("video_url"),
     video_url: Schema.Struct({ url: Schema.String }),
   }),
   ```

4. Les modèles ciblés déclarent bien l'entrée vidéo (catalogue embarqué
   `packages/core/src/models-dev/snapshot.txt`, entrées `opencode-go/gemini-4-argon` et
   `commandcode/google/gemini-3.6-flash`) :

   ```json
   "input": ["text","image/png","image/jpeg","image/webp","image/heic","image/heif",
             "video/mp4","video/mpeg","video/mov","video/avi","video/x-flv","video/mpg",
             "video/webm","video/wmv","video/3gpp","audio/…","application/pdf"]
   ```

   `packages/core/src/models-dev.ts` réduit ces MIME exacts à la modalité large :

   ```ts
   function modality(value: string) {
     if (value === "application/pdf") return "pdf"
     const kind = value.split("/")[0]
     return kind === "text" || kind === "image" || kind === "audio" || kind === "video" ? kind : value
   }
   ```

   donc `Model.Capabilities.input` contient `"video"` et le garde-fou de fallback existe déjà (§4).

5. La doc Console confirme que la passerelle accepte `video_url` en media inline pour les modèles qui
   ont l'entrée vidéo : « Pass images, audio, video, and PDFs in `state` as chat-style parts
   (`image_url`, `input_audio`, `video_url`, `file`) » (<https://opencode.ai/docs/zen>, section
   *GPT 6 Luna Decision and Gemini Decision 1*).

**Ce que le transport ne prouve pas** : que chaque modèle accepte *ce conteneur* (mp4/webm/mov) à
cette taille. L'acceptation serveur se mesure en e2e (`t4`), pas en spec. Format retenu :
**partie `{"type":"video_url","video_url":{"url":"data:video/mp4;base64,…"}}`** dans le message
utilisateur, pour les trois providers (un seul chemin de code : `openai-compatible` chat).

**Conséquence sur le brief** : « opencode / zen » et « openai-compatible commandcode » ne sont pas
deux transports différents ici — c'est le même protocole, résolu par `models.dev` + `aisdk-native`.
Un seul endroit à toucher.

## 1. Constats, avec extraits du code cloné

### 1.a Admission des pièces jointes (détection par octets, cap 20 MiB)

`packages/core/src/mime.ts` — détection par octets. Le conteneur MP4/QuickTime est déjà reconnu,
**WebM ne l'est pas** (RIFF/WEBM tombe dans `isText` puis `application/octet-stream`) :

```ts
export function detect(bytes: Uint8Array) {
  return detectMedia(bytes) ?? (isText(bytes) ? "text/plain" : "application/octet-stream")
}

export function detectMedia(bytes: Uint8Array) {
  …
  if (startsWith(bytes, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return "image/png"
  if (startsWith(bytes, [0xff, 0xd8, 0xff])) return "image/jpeg"
  const signature = ascii(bytes, 0, 6)
  if (signature === "GIF87a" || signature === "GIF89a") return "image/gif"
  …
  if (startsWith(bytes, [0x25, 0x50, 0x44, 0x46, 0x2d])) return "application/pdf"
  if (startsWith(bytes, [0x52, 0x49, 0x46, 0x46]) && startsWith(bytes.subarray(8), [0x57, 0x45, 0x42, 0x50]))
    return "image/webp"
  if (bytes.length >= 12 && ascii(bytes, 4, 8) === "ftyp") {
    …
    if (brand === "qt  ") return "video/quicktime"
    if (brand === "M4A ") return "audio/mp4"
    return "video/mp4"
  }
  …
}
```

`packages/core/src/session/prompt.ts` — admission, cap, et le seul point où le type de fichier
décide de la suite :

```ts
const materializeAttachment = Effect.fn("SessionPrompt.materializeAttachment")(function* (
  input: PromptInput.FileAttachment,
) {
  …
  const label = attachmentLabel(input)
  const resolved = input.uri.startsWith("data:")
    ? {
        bytes: yield* decodeDataURL(input.uri, label),
        …
      }
    : yield* readFileAttachment(input.uri)
  if (resolved.bytes.byteLength > MAX_ATTACHMENT_BYTES)
    return yield* new AttachmentError({
      uri: label,
      message: `Attachment exceeds the ${MAX_ATTACHMENT_BYTES} byte limit: ${label}`,
    })

  const mime = resolved.mime ?? Mime.detect(resolved.bytes)
  …
})

const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
```

et le pré-contrôle de taille sur le stat, avant lecture :

```ts
  if (info.type !== "File") return yield* new AttachmentError({ uri, message: `Attachment is not a file: ${uri}` })
  if (Number(info.size) > MAX_ATTACHMENT_BYTES)
    return yield* new AttachmentError({
      uri,
      message: `Attachment exceeds the ${MAX_ATTACHMENT_BYTES} byte limit: ${uri}`,
    })
```

### 1.b Où le binaire non supporté est écarté de la requête modèle (le vrai point de panne)

`packages/core/src/session/runner/to-llm-message.ts` — la liste blanche de l'unique fonction qui
transforme un attachement en contenu modèle. Tout ce qui n'y figure pas tombe dans `return []` :

```ts
const imageMimes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"])

const media = (file: FileAttachment): MediaPart => ({
  type: "media",
  media: Media.base64(file.data, file.mime),
  filename: file.name,
})

const attachmentContent = (file: FileAttachment): Array<TextPart | MediaPart> => {
  if (file.mime === "text/plain") return [textAttachment(file)]
  if (file.mime === "application/x-directory") return [directoryAttachment(file)]
  if (imageMimes.has(file.mime) || file.mime === "application/pdf") {
    const location = attachmentLocation(file)
    return [...(location === undefined ? [] : [Message.text(`Attached file: ${location}`)]), media(file)]
  }
  return []
}
```

C'est exactement ce que dit la doc V2 (<https://opencode.ai/v2/docs/attachments>, onglet *Formats*) :
« AVIF, BMP, audio, video, and other binary prompt attachments are not included in the model request. »
Une vidéo admise au §1.a ne produit donc **rien** dans la requête aujourd'hui, sans erreur.

`attachmentContent` est appelé par `userAttachmentContent`, qui ne déduplique que les images inline
mentionnées deux fois — un ajout vidéo n'y change rien (voir §2.2).

### 1.c Modèle de partie de message et persistance

Schéma (`packages/schema/src/prompt.ts`) — `mime` est une chaîne libre, donc **rien à changer pour
persister une vidéo** :

```ts
export const FileAttachment = Schema.Struct({
  data: Base64,
  mime: Schema.String,
  source: FileSource,
  name: Schema.String.pipe(optional),
  description: Schema.String.pipe(optional),
  mention: PromptMention.pipe(optional),
})
  .annotate({ identifier: "Prompt.FileAttachment" })
  …
```

Persistance (`packages/core/src/session/stored.ts`) — même forme, ou un `media:` ref vers la table
`blob` quand le flag `media_blob` est actif :

```ts
const { data: _, ...attachment } = FileAttachment.fields

/** A user file whose bytes live in `blob`. */
export const FileRef = Schema.Struct({ ...attachment, uri: Ref })

export const File = Schema.Union([FileAttachment, FileRef])
```

`packages/core/src/session/blob.ts` ne filtre aucun MIME (`take(pending, raw, mime)` stocke tel quel,
`inlineFile` reconstruit `data`). La partie modèle est créée à la volée par
`Media.base64(file.data, file.mime)` (§1.b) : **aucun nouveau type de partie n'est nécessaire**, la
partie `media` transporte déjà `kind: "video"` via `Media.kindOf`.

```ts
export const kindOf = (mediaType: string): Kind => {
  const lower = mediaType.toLowerCase()
  if (lower.startsWith("image/")) return "image"
  if (lower.startsWith("video/")) return "video"
  …
}
```

### 1.d Fallback « modèle sans support » (déjà écrit, à réutiliser tel quel)

`packages/core/src/session/model-request.ts` — la modalité `video` est déjà mappée, et la note courte
est déjà celle qui sert au PDF sans entrée `pdf` :

```ts
const mimeToModality = (mime: string) => {
  if (mime.startsWith("image/")) return "image"
  if (mime.startsWith("audio/")) return "audio"
  if (mime.startsWith("video/")) return "video"
  if (mime === "application/pdf") return "pdf"
}
…
const unsupportedMedia = (
  mime: string,
  name: string | undefined,
  capabilities: Model.Capabilities,
  provider: string | undefined,
) => {
  const modality = mimeToModality(mime)
  if (!modality) return
  const unsupported = !capabilities.input.some((item) => item.startsWith(modality))
    ? modality
    : provider === "xai" && modality === "image" && !XAI_IMAGE_MIMES.has(mime.toLowerCase())
      ? mime
      : undefined
  if (!unsupported) return
  return `ERROR: Cannot read ${name ? `"${name}"` : modality} (this model does not support ${unsupported} input). Inform the user.`
}
```

Appliqué à chaque requête, avant l'envoi :

```ts
        messages: boundImages(
          unsupportedParts(
            shaped.messages.map((message) => {
              … }),
            model.capabilities,
            model.model.provider,
          ),
        ),
```

Donc : **dès qu'une vidéo devient une `MediaPart`, un modèle sans `"video"` dans
`capabilities.input` reçoit automatiquement la note courte**, sans une ligne de plus. C'est la preuve
que le fallback du brief n'est pas à écrire, seulement à ne pas contourner.

### 1.e Config `media.*` et son schéma

`packages/schema/src/config/media.ts` — n'accueille que l'image :

```ts
export * as ConfigMedia from "./media.js"

import { Schema } from "effect"
import { optional, PositiveInt } from "../schema.js"

export class Image extends Schema.Class<Image>("Config.Media.Image")({
  auto_resize: Schema.Boolean.pipe(optional),
  max_width: PositiveInt.pipe(optional),
  max_height: PositiveInt.pipe(optional),
  max_base64_bytes: PositiveInt.pipe(optional),
}) {}

export class Info extends Schema.Class<Info>("Config.Media")({
  image: Image.pipe(optional),
}) {}
```

Consommée par un plugin (`packages/core/src/config/plugin/image.ts`), qui pousse la config dans le
`State` du service `Image` :

```ts
    const loaded = yield* ConfigEntryObserver.observe(config, ctx.event, image.reload())
    yield* image.transform((editor) => {
      for (const entry of loaded.entries) {
        if (entry.type !== "document") continue
        const configured = entry.info.media?.image
        if (!configured) continue
        editor.configure({
          ...(configured.auto_resize === undefined ? {} : { autoResize: configured.auto_resize }),
          …
        })
      }
    })
```

Lecture ponctuelle de config à la même échelle que le besoin vidéo, dans le style déjà employé par
`packages/core/src/session/projector.ts` :

```ts
const partRowsEnabled = Effect.gen(function* () {
  const config = yield* Effect.serviceOption(Config.Service)
  if (Option.isNone(config)) return true
  return Config.latest(yield* config.value.entries(), "experimental")?.part_rows !== false
})
```

### 1.f Chemins client déjà bons (rien à changer)

- `packages/cli/src/run/run.ts` : un fichier non textuel part en data URL, MIME déduit puis
  re-détecté par les octets côté serveur.

  ```ts
  const mime =
    detected.startsWith("image/") || detected === "application/pdf"
      ? detected
      : !isBinaryContent(bytes) && Buffer.from(text, "utf8").equals(bytes)
        ? "text/plain"
        : detected
  const name = path.basename(file)
  if (mime === "text/plain") return { text: `<file name="${name}">\n${text}\n</file>` }
  return { attachment: { uri: `data:${mime};base64,${bytes.toString("base64")}`, name } }
  ```

  avec `const ATTACH_FILE_MAX_BYTES = 10 * 1024 * 1024` (limite **du client CLI**, à ne pas confondre
  avec le cap serveur, et à documenter — cf. §3.3).
- `packages/tui/src/component/prompt/local-attachment.ts` : la table d'extensions ne connaît que
  l'image/PDF, donc un `.mp4` collé arrive en `application/octet-stream` ; les octets sont malgré tout
  re-détectés côté serveur par `Mime.detect`, donc **ça fonctionne**, l'extension n'ajoutant qu'un
  MIME décoratif. Ajout facultatif (§2.5).

## 2. Plan d'implémentation

Cinq fichiers de production, un de test. Extraits « avant » copiés du clone ; chaque remplacement est
copié tel quel dans le patch.

### 2.1 `packages/core/src/mime.ts` — détecter WebM

Après la branche WebP, ajouter :

```ts
  if (startsWith(bytes, [0x52, 0x49, 0x46, 0x46]) && startsWith(bytes.subarray(8), [0x57, 0x45, 0x42, 0x4d]))
    return "video/webm"
```

(`52 49 46 46` = `RIFF`, `57 45 42 4d` = `WEBM`.) MP4 (`ftyp`) et QuickTime (`qt  `) sont déjà
détectés : ne pas y toucher.

### 2.2 `packages/core/src/session/runner/to-llm-message.ts` — admettre la vidéo dans la requête

Remplacer la liste unique par un ensemble média (les images gardent leur sémantique propre pour la
déduplication, §`userAttachmentContent`) :

```ts
const imageMimes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"])
// Video reaches the provider as inline media, the way images do; a model without video input gets the
// short note from `SessionModelRequest.unsupportedMedia` instead.
const videoMimes = new Set(["video/mp4", "video/webm", "video/quicktime"])
const mediaMimes = new Set([...imageMimes, ...videoMimes])
```

puis, dans `attachmentContent`, remplacer la condition :

```ts
  if (imageMimes.has(file.mime) || file.mime === "application/pdf") {
```

par :

```ts
  if (mediaMimes.has(file.mime) || file.mime === "application/pdf") {
```

`userAttachmentContent` reste inchangé : la vidéo n'est jamais dédupliquée par mention, ce qui est le
comportement voulu (deux vidéos identiques jointes deux fois restent deux pièces jointes, comme deux
PDF).

### 2.3 `packages/schema/src/config/media.ts` — `media.video`

```ts
export class Video extends Schema.Class<Video>("Config.Media.Video")({
  /** Decoded bytes allowed per video attachment. Defaults to the 20 MiB attachment limit. */
  max_bytes: PositiveInt.pipe(optional),
}) {}

export class Info extends Schema.Class<Info>("Config.Media")({
  image: Image.pipe(optional),
  video: Video.pipe(optional),
}) {}
```

`media.image` garde exactement sa forme et son défaut : `media.video` est purement additif, un fichier
de config existant continue de décoder.

### 2.4 `packages/core/src/session/prompt.ts` — cap par type, configurable

Imports à compléter : `Config` depuis `../config.js` (`Option` est déjà importé).

Sous `const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024`, ajouter la limite effective et le plafond de
lecture :

```ts
// A video may be larger than the general attachment limit, but only when the config says so. The
// stat pre-check reads against the largest allowance; the per-type limit applies after the sniff.
const attachmentCeiling = Effect.fn("SessionPrompt.attachmentCeiling")(function* () {
  const config = yield* Effect.serviceOption(Config.Service)
  if (Option.isNone(config)) return MAX_ATTACHMENT_BYTES
  const max = Config.latest(yield* config.value.entries(), "media")?.video?.max_bytes
  return max === undefined ? MAX_ATTACHMENT_BYTES : Math.max(MAX_ATTACHMENT_BYTES, max)
})

const attachmentLimit = Effect.fn("SessionPrompt.attachmentLimit")(function* (mime: string) {
  if (!mime.startsWith("video/")) return MAX_ATTACHMENT_BYTES
  const config = yield* Effect.serviceOption(Config.Service)
  if (Option.isNone(config)) return MAX_ATTACHMENT_BYTES
  return Config.latest(yield* config.value.entries(), "media")?.video?.max_bytes ?? MAX_ATTACHMENT_BYTES
})
```

Dans `materializeAttachment`, remplacer le bloc

```ts
  if (resolved.bytes.byteLength > MAX_ATTACHMENT_BYTES)
    return yield* new AttachmentError({
      uri: label,
      message: `Attachment exceeds the ${MAX_ATTACHMENT_BYTES} byte limit: ${label}`,
    })

  const mime = resolved.mime ?? Mime.detect(resolved.bytes)
```

par (le sniff passe **avant** le cap, pour que la limite dépende du type réel) :

```ts
  const mime = resolved.mime ?? Mime.detect(resolved.bytes)
  const limit = yield* attachmentLimit(mime)
  if (resolved.bytes.byteLength > limit)
    return yield* new AttachmentError({
      uri: label,
      message: `Attachment exceeds the ${limit} byte limit: ${label}`,
    })
```

et dans `readFileAttachment`, remplacer le pré-contrôle

```ts
  if (Number(info.size) > MAX_ATTACHMENT_BYTES)
```

par

```ts
  const ceiling = yield* attachmentCeiling()
  if (Number(info.size) > ceiling)
```

pour ne pas refuser un gros fichier avant d'avoir pu constater que c'est une vidéo.

Aucun changement à `normalizeImageAttachment` : `RESIZABLE_IMAGE_MIMES` ne contient pas `video/*`,
donc une vidéo passe telle quelle (pas de redimensionnement possible, et c'est voulu).

### 2.5 (facultatif, zéro risque) `packages/tui/src/component/prompt/local-attachment.ts`

Ajouter trois entrées à `mimeTypes`, pour que l'URL data du TUI porte le bon type :

```ts
  ".mp4": "video/mp4",
  ".mov": "video/quicktime",
  ".webm": "video/webm",
```

Non requis pour le DONE (le serveur re-détecte par les octets) ; à inclure seulement si `t2` garde le
patch minimal sur core/schema.

## 3. Limites levées et limites connues

### 3.1 Cap 20 MiB

Avant : `MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024` s'applique à tout, y compris à une future vidéo.
Après : 20 MiB reste le défaut pour texte/image/PDF ; une vidéo suit `media.video.max_bytes`
(plafond de lecture = `max(20 MiB, media.video.max_bytes)`). Le défaut ne change pour personne.

### 3.2 `media.video`

Nouveau champ, purement additif, sur le modèle de `media.image`. Documenté comme une limite de
traitement, pas une frontière de sécurité (même avertissement que la doc image).

### 3.3 Limites qui restent, à écrire dans la PR et dans `test-e2e.md`

1. **Client CLI** : `opencode run --file` refuse au-delà de `ATTACH_FILE_MAX_BYTES = 10 * 1024 * 1024`.
   Une vidéo > 10 MiB ne passe pas par la CLI, même si le serveur l'accepterait.
2. **Budget inline** : `boundImages` ne compte que `image/*` (déclencheur 25 MiB, cible 15 MiB). Une
   vidéo n'est jamais retirée d'un historique long ; elle est donc renvoyée à chaque étape, bornée par
   `media.video.max_bytes`. Décision assumée : ne pas toucher `boundImages` (un élargissement changerait
   la politique des images, donc régression possible sur l'existant).
3. **Estimation de tokens** : `estimateMedia` dans `packages/core/src/session/compaction.ts` renvoie 0
   pour la vidéo (comme aujourd'hui pour l'audio), donc la compaction sous-estime une requête vidéo.
4. **Format** : seuls `video/mp4`, `video/webm`, `video/quicktime` sont admis ; un `.avi`/`.mkv` reste
   écarté comme avant (aucun ajout de type implicite).

## 4. Non-régression (cas nommé du DONE)

- PNG / JPEG / GIF / WebP : `mediaMimes` contient toujours `imageMimes` à l'identique, `boundImages` et
  `RESIZABLE_IMAGE_MIMES` ne bougent pas.
- PDF : branche `application/pdf` inchangée, y compris le fallback « modèle sans entrée pdf ».
- Texte / répertoire : branches `text/plain` et `application/x-directory` inchangées.
- Config : `media.image` garde champ pour champ sa forme ; un `opencode.json` sans `media` décode
  comme avant.
- Le cas nommé de `t4` (PNG puis PDF ré-attachés → traitement normal) est le contrôle de ces trois
  points.

## Build

Toolchain : **bun 1.4.4 uniquement** (`/media/marcs/Linux_Apps/Projets_AI/Bun/bin/bun`). `node` est
absent de la machine et **n'est pas nécessaire** : `package.json` déclare
`"packageManager": "bun@1.4.4"`, `postinstall` est un script bun, et le compilateur est
`Bun.build({ compile })` (`packages/cli/script/build.ts`) — aucun `node` n'est invoqué. Ne rien
installer.

Le build produit un binaire autonome par cible dans
`packages/cli/dist/cli-<os>-<arch>[-baseline][-musl]/bin/opencode`
(`outfile: path.join(outdir, name, "bin", binary)`, `name = target.replace(binary, "cli")`,
`target = opencode-linux-x64` sur cette machine).

Commandes exactes, dans cet ordre :

```bash
cd /media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode-src
PATH=/media/marcs/Linux_Apps/Projets_AI/Bun/bin:$PATH bun install
cd packages/cli
PATH=/media/marcs/Linux_Apps/Projets_AI/Bun/bin:$PATH bun run script/build.ts --single --skip-web-ui --skip-install
```

- `bun install` à la racine : monorepo bun (`bun.lock`), installe les workspaces ; sans lui le build
  n'a pas de `node_modules`.
- `--single` : ne compile que la cible de la machine (`linux-x64` ici), au lieu des 12 cibles.
- `--skip-web-ui` : n'embarque pas l'archive de l'app web (build vite de `packages/app`), inutile pour
  la CLI/TUI V2 ; l'archive embarquée est alors vide. Retirer ce drapeau si `t3` veut un binaire
  strictement identique à la release (build plus long, aucune dépendance à `node` non plus).
- `--skip-install` : évite le `bun install --os="*" --cpu="*" --libc="*"` multi-plateformes du script,
  inutile pour une cible unique.

**Sortie attendue (chemin source pour `t3`)** :

```
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode-src/packages/cli/dist/cli-linux-x64/bin/opencode
```

**Validé le 2026-11-24 sur ce clone, sans aucun `node`** : `bun install` → `1586 packages installed
[156.65s]`, exit 0 (3 postinstalls bloqués par bun, sans effet sur ce build) ; puis les trois
commandes ci-dessus → exit 0, `building cli-linux-x64`, binaire ELF x86-64 de 267 MB dans
`packages/cli/dist/cli-linux-x64/bin/opencode`, qui répond
`opencode v0.0.0-dev-202611241340` à `--version`. `t3` n'a donc aucune dépendance à installer.

`t3` copie ce fichier vers `/media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode` (après sauvegarde
`opencode.bak-20261124-pre-video`), `chmod +x`, puis vérifie via le lanceur
`/media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode-v2 --version` (le lanceur `exec` simplement
`$BASE/opencode`, aucun changement à y faire).

Vérification rapide alternative avant le binaire complet (facultative, plus rapide) :

```bash
cd /media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode-src/packages/core
PATH=/media/marcs/Linux_Apps/Projets_AI/Bun/bin:$PATH bun typecheck
```

## Tests

Conventions du dépôt (lues telles quelles) :

- `bunfig.toml` racine : `[test] root = "./do-not-run-tests-from-root"` — **on ne lance pas la suite
  depuis la racine** (`"test": "echo 'do not run tests from root' && exit 1"`).
- `packages/core/package.json` : `"test": "bun run script/test.ts"`, où `script/test.ts` spawn
  `bun test --only-failures --timeout=30000 …` avec un HOME/XDG isolé (`packages/core/bunfig.toml`
  charge `./test/preload.ts`).
- Les tests unitaires vivent dans `packages/core/test/*.test.ts` (`mime.test.ts`,
  `session-model-request.test.ts`, `session-prompt.test.ts` en sont les voisins directs).

**Fichier de test imposé par la carte `t2`** : `packages/core/test/attachment-video.test.ts`

**Commande exacte (depuis le paquet, jamais depuis la racine)** :

```bash
cd /media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode-src/packages/core
PATH=/media/marcs/Linux_Apps/Projets_AI/Bun/bin:$PATH bun run test test/attachment-video.test.ts
```

Équivalent direct du runner, si `script/test.ts` gêne :

```bash
cd /media/marcs/Linux_Apps/Projets_AI/Opencodev2/opencode-src/packages/core
PATH=/media/marcs/Linux_Apps/Projets_AI/Bun/bin:$PATH bun test test/attachment-video.test.ts
```

**Validé le 2026-11-24** avec le voisin direct (`test/mime.test.ts`) : `bun run test test/mime.test.ts`
→ `9 pass, 0 fail`. Le runner du paquet fonctionne, le chemin de fichier en argument aussi.

Contenu minimal du test (trois assertions, aucune mock, test de l'implémentation réelle) :

1. **Détection par octets** — `Mime.detect` sur des en-têtes réels : `ftyp isom` → `video/mp4`,
   `ftyp qt  ` → `video/quicktime`, `RIFF…WEBM` → `video/webm` (la branche ajoutée en §2.1), et
   contrôle non-régression `RIFF…WEBP` → `image/webp`.
2. **Admission dans la requête** — `toLLMMessages([userMessage], modelRef)` avec un
   `FileAttachment` `mime: "video/mp4"` : la sortie contient une partie `{ type: "media" }` dont
   `media.kind === "video"`, plus le texte `Attached file: …` quand la source est `uri`.
3. **Fallback note courte** — `SessionModelRequest.unsupportedParts(messages, { input: ["text","image"],
   … }, "opencode-go")` remplace cette partie média par le texte
   `ERROR: Cannot read "clip.mp4" (this model does not support video input). Inform the user.` ; et
   `unsupportedParts` avec `input: ["text","video"]` la laisse intacte.

Ordre de relecture recommandé pour `t2` : lancer d'abord `mime.test.ts` (voisin, doit rester vert),
puis `attachment-video.test.ts`, puis `bun typecheck` dans `packages/core`.

## 5. Points ouverts transmis (pas des décisions de la spec)

1. **Vérif de `t2`** : `bun run build` à la racine du clone **n'existe pas** (aucun script `build` dans le
   `package.json` racine ; `bun run test` y échoue volontairement). La commande de build est
   celle de la section `## Build` ci-dessus, et la commande de test celle de `## Tests`.
2. **Modèle e2e de `t4`** : `opencode-go/qwen3.6-plus-free` **n'existe plus** au catalogue
   (`packages/core/src/models-dev/snapshot.txt` : `opencode-go` n'a que `crouton-alpha-free` et
   `space-bunny` en entrée vidéo ; le catalogue live en ajoute, dont `qwen3.7-plus`,
   `minimax-m3`, `gemini-4-argon`). Choisir le modèle e2e en vérifiant d'abord l'entrée vidéo dans le
   catalogue effectif (`opencode models`), pas dans le brief.
3. **Documentation utilisateur** : la page `v2/docs/attachments` affirme encore « video … not included
   in the model request ». La PR upstream devra inclure la mise à jour de cette page (hors périmètre
   de ce dépôt, à mentionner dans `PR-UPSTREAM.md`).
