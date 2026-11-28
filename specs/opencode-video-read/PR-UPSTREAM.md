[BROUILLON — NON ENVOYÉ] Brouillon de pull request pour anomalyco/opencode — aucun envoi, aucune publication : Marc publiera lui-même depuis sa branche.

---

**Titre proposé (conventional commits)**

```
feat: forward video to models end-to-end (attachments, read tool, 20 MiB --file)
```

**Base :** `dev` @ `678621a` (`package.json` `2.4.1`). Portée : `packages/core`, `packages/schema`, `packages/cli`, `packages/cli-rs`. Aucun changement dans les protocoles de transport.

The PR ships one feature in two parts: (1) the attachment patch — video is admitted as model content (previous diff, 5 files); (2) this patch — the `read` tool ingests video (`MEDIA_MIMES` extended), the CLI `--file` ceiling goes from 10 to 20 MiB with messages derived from the constant, TS/Rust parity stays aligned, and the `read` success label follows the MIME type (Video / PDF / Image).

---

## Motivation

Three separate locks block video, and each one fails silently or with a hard refusal:

1. **Video attachments were silently dropped before the model request.** `Mime.detect` already recognised MP4/QuickTime/WebM and the 20 MiB attachment cap already applied, but `attachmentContent` in `packages/core/src/session/runner/to-llm-message.ts` kept a whitelist of four image MIME types plus `application/pdf` and returned `[]` for everything else — a model declaring video input (Gemini, Qwen…) never received the bytes, with no error explaining why.
2. **The `read` tool refused every binary that was not an image or a PDF.** `MEDIA_MIMES` in `packages/core/src/tool/read-filesystem.ts` listed `image/png`, `image/jpeg`, `image/gif`, `image/webp` and `application/pdf`; a `.mp4` hit `BinaryFileError` even though the transport could carry it.
3. **`--file` capped attachments at 10 MiB** (`ATTACH_FILE_MAX_BYTES` in `packages/cli/src/run/run.ts` and `packages/cli-rs/crates/cli/src/run/files.rs`) — below the server's own 20 MiB attachment limit, so a video the server would happily accept could never be attached from the CLI, and both refusal messages hard-coded "10 MiB" instead of deriving the figure from the constant.

## Changes

Grouped by lock; the file list is the full diff (12 files).

### 1. Video attachments reach the model request (previous patch)

- **`packages/core/src/mime.ts`** — detect WebM by its bytes (`RIFF…WEBM` used to fall through to `application/octet-stream`); MP4 and QuickTime were already detected via their `ftyp` brand.
- **`packages/core/src/session/runner/to-llm-message.ts`** — `videoMimes` (`video/mp4`, `video/webm`, `video/quicktime`) combined into a `mediaMimes` set with the image set; a video becomes a `media` part exactly as an image does. The image set keeps its own meaning for `userAttachmentContent`'s deduplication. `Media.kindOf` already returns `"video"` for `video/*`, so no new part type is needed.
- **`packages/schema/src/config/media.ts`** — additive `media.video.max_bytes` (optional positive int, defaults to the 20 MiB attachment limit); `media.image` and existing config files unchanged.
- **`packages/core/src/session/prompt.ts`** — per-type size handling: `attachmentCeiling()` returns `max(20 MiB, media.video.max_bytes)` and guards the `stat` pre-check in `readFileAttachment`; `attachmentLimit(mime)` returns the 20 MiB default for anything that is not `video/*` and guards the post-sniff check in `materializeAttachment` (sniff now runs before the size check). Both refusal messages name the limit that actually applied, derived at runtime.
- **`packages/core/test/attachment-video.test.ts`** — new (86 lines): container detection, media-part conversion, fallback note, part kept when the model declares video input.

### 2. The `read` tool ingests video (this patch)

- **`packages/core/src/tool/read-filesystem.ts`** — `MEDIA_MIMES` extended with `video/mp4`, `video/webm`, `video/quicktime`; a new `mediaCeiling(mime)` reads `media.video.max_bytes` from config only for `video/*` (a context without `Config` keeps the default), so the ingest limit check after the sniff applies the right bound and `MediaIngestLimitError` reports that bound.
- **`packages/core/src/tool/plugin/read.ts`** — `toModelContent` labels the success text by MIME: `Video read successfully` for `video/*`, `PDF read successfully` for `application/pdf`, `Image read successfully` otherwise (the label previously said "Image" for every non-PDF media).
- **`packages/core/test/read-video.test.ts`** — new (64 lines): `MEDIA_MIMES` contains images + PDF + the three video containers; a 32-byte ISO BMFF header reads as `type: "media"` with real bytes; a binary `archive.dat` is still refused with `BinaryFileError`.

### 3. `--file` ceiling: 10 → 20 MiB, messages derived from the constant (this patch)

- **`packages/cli/src/run/run.ts`** — `ATTACH_FILE_MAX_BYTES = 20 * 1024 * 1024`, exported so tests assert the same value; the refusal message derives `ATTACH_FILE_MAX_BYTES / (1024 * 1024)` instead of hard-coding "10 MiB". (The CLI has no Config service in scope, so it keeps the prompt patch's 20 MiB default.)
- **`packages/cli-rs/crates/cli/src/run/files.rs`** — same constant bump to 20 MiB and same derived message; the Rust unit test derives its expected string from the constant too.
- **`packages/cli/test/run/process.test.ts`** — the expectation imports `ATTACH_FILE_MAX_BYTES` and interpolates the MiB figure (4/4 pass).
- **`packages/cli-rs/parity/scenarios/C.10/file-large.cli.ts`** — the scenario builds its oversized file from the TS constant (`ATTACH_FILE_MAX_BYTES + 1`) instead of restating `10 * 1024 * 1024`, so parity can never drift from the guard it exercises.

## Testing

Unit (run from `packages/core` / `packages/cli` as appropriate):

| Suite | Result |
| --- | --- |
| `bun run test test/read-video.test.ts` (new) | 3 pass, 0 fail |
| `bun run test test/tool-read-filesystem.test.ts` | 48 pass, 0 fail (including the refusal of a binary `.dat`) |
| `bun run test test/run/process.test.ts` (cli) | 4 pass, 0 fail (message reads "larger than 20 MiB") |
| Parity scenario `C.10` (`file-large`) | exit 1 + "Cannot attach … larger than 20 MiB" |
| Non-regression: `attachment-video` 53/53, `mime` 9/9, `session-prompt` 52/52, core CLI 432/432, typecheck 0 errors | green |

Real end-to-end run against a binary built from this patch (`bun run script/build.ts --single --skip-web-ui`, `v0.0.0-dev-202611281532`), on a real video of **14.19 MiB** (`aetherapparel.com_dm.mp4`, 14 884 724 o, 37.9 s, 1080×1920, h264+aac — above the old 10 MiB ceiling, **no re-encoding beforehand**):

- **`read` direct** (`opencode run … "Lis ce fichier avec l'outil read …, sans ré-encodage ni conversion"`): the tool logged `Read …/aetherapparel.com_dm.mp4`, returned `mime: video/mp4` from the original bytes, no `MediaIngestLimitError`, no `ffmpeg` in that pass. The description (EU-only size grid 35→47, `BUTTON AT 0.4712 · NOTE BELOW`, split BEFORE/AFTER, end card `7 FREE REDESIGNS · 48 HOURS · ↓ LINK BELOW`) matches the actual video content, the poster frame, and the `_post.md` brief.
- **`--file` with the same original** (CLI path, ceiling now 20 MiB): `EXIT=0`, no "file larger than 20 MiB" refusal on a 14.19 MiB file, and a description congruent with the video (same page, same three UX defects, same end card).
- **Fallback on a model without video input** (`opencode-go/deepseek-v4.1-flash`): `read` still returns the media part (14.88 MB ingested), the attachment layer substitutes `this model does not support video input`, the model says so, the run ends with `EXIT=0` — no crash, no attachment error.
- **Non-regression**: a JPEG poster read through `read` returns `Image read successfully` and correct content; a one-page PDF read through `read` returns `application/pdf` media with the right text (via `gemini-4-argon`); PDF on non-PDF models falls back with `this model does not support pdf input`, `EXIT=0`.

## Known limitations

- **Rust modified but NOT compiled.** `rustc` is absent from the local toolchain (`rustup` carries only `cargo`/`clippy`); `rustup component add rustc` must be run before submitting. The `files.rs` change (constant + derived message) and the C.10 scenario were reviewed statically and exercised through the TS side only.
- **The ceiling is proven to 14.19 MiB only.** No file between 14.19 and 20 MiB, and no rejection above 20 MiB, was replayed in this run; the guard branch beyond the constant is read, not measured.
- **On a natural instruction the model may prefer a video MCP that re-encodes.** In the first end-to-end attempt, the model chose the `video-analyzer` MCP and re-encoded with `ffmpeg -crf 30` to stay under its own ingest limit; the no-re-encoding path is only guaranteed when `read` is explicitly requested. This is a model/tool-choice behaviour, not a property of the patch.
- **A multimodal model sometimes emits the `read` call as prose text** (`… tools.read(input) …`) instead of a tool call; the run then stops without an answer (roughly 1 attempt in 3 in testing). Simple re-issue, not a patch defect.

## Documentation

`v2/docs/attachments` must be updated **in the same PR**: the *Formats* paragraph still states that video prompt attachments are not included in the model request, and the config reference should mention `media.video.max_bytes` next to `media.image`.

---

*Diff: 12 files, 471 lines of patch (`packages/cli-rs/crates/cli/src/run/files.rs`, `packages/cli-rs/parity/scenarios/C.10/file-large.cli.ts`, `packages/cli/src/run/run.ts`, `packages/cli/test/run/process.test.ts`, `packages/core/src/mime.ts`, `packages/core/src/session/prompt.ts`, `packages/core/src/session/runner/to-llm-message.ts`, `packages/core/src/tool/plugin/read.ts`, `packages/core/src/tool/read-filesystem.ts`, `packages/core/test/attachment-video.test.ts` (new), `packages/core/test/read-video.test.ts` (new), `packages/schema/src/config/media.ts`).*
