[BROUILLON — NON ENVOYÉ] Brouillon de pull request pour anomalyco/opencode — aucun envoi, aucune publication : Marc publiera lui-même depuis sa branche.

---

**Titre proposé (conventional commits)**

```
feat(attachments): pass video attachments to models that declare video input
```

**Base :** `dev` @ `678621a` (`package.json` `2.4.1`). Portée : `packages/core`, `packages/schema`. Aucun changement dans `packages/ai` ni dans les protocoles de transport.

---

## Motivation

A video attachment is admitted today and then silently dropped before the model request.

`Mime.detect` already recognises MP4 (`ftyp`), QuickTime (`qt  `) and the 20 MiB attachment cap applies, so the file reaches the session as a `FileAttachment` with `mime: "video/mp4"`. But the only function that turns an attachment into model content — `attachmentContent` in `packages/core/src/session/runner/to-llm-message.ts` — keeps a whitelist of four image MIME types plus `application/pdf`, and everything else returns `[]`:

```ts
const imageMimes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"])
…
  if (imageMimes.has(file.mime) || file.mime === "application/pdf") { … }
  return []
```

The consequence is that a model which declares video input (Gemini, Qwen, and others in the models.dev catalogues of the `opencode`, `opencode-go` and `commandcode` providers) never receives the bytes, and no error tells the user why: the attachment simply disappears from the request. The same is true, word for word, in the v2 documentation page (`v2/docs/attachments`, *Formats*): "AVIF, BMP, audio, video, and other binary prompt attachments are not included in the model request."

The transport already supports it. All three providers resolve to `@ai-sdk/openai-compatible` → `OpenAIChat.protocol`, whose media lowering already emits a video part:

```ts
if (part.media.kind === "video") return { type: "video_url" as const, video_url: { url } }
```

and whose request schema already admits `{"type":"video_url","video_url":{"url": …}}`. The fallback for models without video input is also already written: `SessionModelRequest.unsupportedMedia` maps `mime.startsWith("video/")` to the `video` modality and, when `capabilities.input` lacks it, replaces the part with the short note used for PDFs on models without PDF input. So the missing piece is admission, not transport.

## Changes

1. **`packages/core/src/mime.ts`** — detect WebM by its bytes. MP4 and QuickTime were already detected through the `ftyp` brand; RIFF/WEBM used to fall through to `application/octet-stream`. One branch added after the WebP one:

   ```ts
   if (startsWith(bytes, [0x52, 0x49, 0x46, 0x46]) && startsWith(bytes.subarray(8), [0x57, 0x45, 0x42, 0x4d]))
     return "video/webm"
   ```

2. **`packages/core/src/session/runner/to-llm-message.ts`** — admit video in the model request. The image set keeps its own meaning (it is what `userAttachmentContent` uses to deduplicate a double-mentioned image), and a combined set is what `attachmentContent` tests:

   ```ts
   const videoMimes = new Set(["video/mp4", "video/webm", "video/quicktime"])
   const mediaMimes = new Set([...imageMimes, ...videoMimes])
   …
   if (mediaMimes.has(file.mime) || file.mime === "application/pdf") { … }
   ```

   A video therefore becomes a `media` part exactly as an image does — inline base64, plus the `Attached file: …` location line when the source is a URI. No new part type is needed: `Media.kindOf` already returns `"video"` for `video/*`.

3. **`packages/schema/src/config/media.ts`** — additive `media.video`:

   ```ts
   export class Video extends Schema.Class<Video>("Config.Media.Video")({
     /** Decoded bytes allowed per video attachment. Defaults to the 20 MiB attachment limit. */
     max_bytes: PositiveInt.pipe(optional),
   }) {}
   ```

   `media.image` keeps its fields and its defaults unchanged, so an existing config file keeps decoding.

4. **`packages/core/src/session/prompt.ts`** — per-type size limit. `attachmentCeiling()` returns `max(20 MiB, media.video.max_bytes)` and guards the `stat` pre-check in `readFileAttachment`, so a large video is not refused before its type can be established; `attachmentLimit(mime)` returns the 20 MiB default for everything that is not `video/*` and `media.video.max_bytes` (or its default) for video, and guards the post-sniff limit in `materializeAttachment`. The sniff now runs before the size check, and both refusal messages name the limit that actually applied (`${ceiling}` / `${limit}`), not a constant.

5. **`packages/core/test/attachment-video.test.ts`** — new test file (see *Testing*).

## Testing

Run from `packages/core` (the repository forbids running the suite from the root):

| Command | Result |
| --- | --- |
| `bun run test test/mime.test.ts` | 9 pass, 0 fail |
| `bun run test test/attachment-video.test.ts` | 5 pass, 0 fail |
| `bun run typecheck` | no type errors |

The new test file covers three things, with no mocks: `Mime.detect` on real container headers (`ftyp isom` → `video/mp4`, `ftyp qt  ` → `video/quicktime`, `RIFF…WEBM` → `video/webm`, and `RIFF…WEBP` still → `image/webp`); `toLLMMessages` turning a `video/mp4` `FileAttachment` into one `media` part with `kind === "video"` and the `Attached file: …` line; and `unsupportedParts` replacing that part with `ERROR: Cannot read "clip.mp4" (this model does not support video input). Inform the user.` for a model with `input: ["text","image"]`, while a model with `["text","video"]` keeps it.

End-to-end, against a binary built from this patch (`bun run script/build.ts --single --skip-web-ui`, `v0.0.0-dev-202611241422`), with a 5 s 640×360 MP4 that shows a red background, a green left band, a cyan `COMPTEUR` title and a white `SECONDE 1`…`SECONDE 5` counter:

- `opencode run --model opencode-go/qwen3.7-plus --file test-video.mp4` (CLI data URL path): the model reported the green band, the red background, the cyan title, the dark banner and all five read as `SECONDE 1`…`SECONDE 5`.
- Same model, same file attached by `file:///` URI through the HTTP API (the `readFileAttachment` path, i.e. the `stat`/ceiling code): the server echo shows `"mime":"video/mp4"` and a `data` payload starting with the real MP4 header (`ftyp isom`), and the description matches the same video content.
- `opencode run --model opencode-go/deepseek-v4.1-flash --file test-video.mp4` (a model without video input): the model received `ERROR: Cannot read "test-video.mp4" (this model does not support video input).` and the run ended normally, with no attachment error.
- Non-regression: a PNG (a frame of the same video) and a PDF (`BANANE BLEUE 42`) attached to `opencode-go/gemini-4-argon` were processed normally, with correct answers and no errors.

## Known limitations

- **Second provider not exercised end to end.** `commandcode` (`google/gemini-3.6-flash`) was not tested: no credentials were available. It runs through the same single code path (`@ai-sdk/openai-compatible` → `OpenAIChat` → `video_url` part); only the endpoint differs. Implemented, not verified.
- **Size at the cap not exercised.** The test video is 15 KB, well below both the 20 MiB default and `media.video.max_bytes`; the ceiling branch (a video larger than 20 MiB accepted through config, and one over its limit refused) is covered by reading the code, not by a run. Note also that the CLI's own `ATTACH_FILE_MAX_BYTES = 10 MiB` still caps `run --file`, so a video above 10 MiB can only be attached through the API/URI path even though the server would accept it.
- **Manual TUI check not run.** Pasting a `.mp4` in the interactive prompt was not exercised; the TUI extension table does not know `.mp4`, so the attachment arrives as `application/octet-stream` and is re-detected by bytes on the server. Expected to work, not measured.
- **Video is never trimmed from long histories.** `boundImages` only bounds `image/*` (25 MiB trigger, 15 MiB target), so a video is re-sent at every step, bounded only by `media.video.max_bytes`. Deliberately not changed here: widening `boundImages` would alter the image policy and risk a regression.
- **Token estimation ignores video.** `estimateMedia` in `packages/core/src/session/compaction.ts` returns 0 for video (as it already does for audio), so compaction under-estimates a request carrying a video.
- **Only three containers.** `video/mp4`, `video/webm` and `video/quicktime` are admitted; `.avi`/`.mkv` stay excluded, as before.

## Documentation

`v2/docs/attachments` still states that video is not included in the model request. That page must be updated in this PR (it lives outside this repository): the *Formats* paragraph should list video among the attachments forwarded to models that declare video input, and the config reference should mention `media.video.max_bytes` next to `media.image`.

---

*Diff: 5 files, 123 insertions, 6 deletions (`packages/core/src/mime.ts`, `packages/core/src/session/runner/to-llm-message.ts`, `packages/core/src/session/prompt.ts`, `packages/schema/src/config/media.ts`, `packages/core/test/attachment-video.test.ts`).*
