# System Specification Document — Unified ACSM Converter

**Version:** 1.1 (approved / locked)
**Status:** All decisions resolved. Implementation pending your explicit "You may start coding."
**Architecture:** Option B — unified Flask app with pluggable format handlers.

---

## 1. Purpose

A single, personal-use web app that accepts an Adobe `.acsm` token, fulfills the
download via libgourou, removes DRM, and returns a DRM-free file — **EPUB or PDF,
auto-detected** — for offline personal reading. This merges the two existing
single-format apps (EPUB-only and PDF-only) into one.

## 2. Scope

**In scope**
- Accept any `.acsm`; auto-route to EPUB or PDF based on the *actual* fulfilled file.
- DRM removal only (decryption-layer; no re-encoding, no transcoding).
- Cover extraction per format; combined library of both formats.
- Single Google-authenticated user; Zeabur deployment.

**Out of scope (explicitly dropped)**
- OCR of any kind, and the entire OCR toolchain (Tesseract, CJK packs, Ghostscript, unpaper, pngquant).
- Multi-user support, shared/public libraries, accounts beyond one allowed email.
- Format conversion between EPUB and PDF.

## 3. Users & Authentication

- **Single user.** Google OAuth2; only the address in `ALLOWED_EMAIL` may log in.
- OAuth flow standardized on App 2's pattern: `APP_BASE_URL` + forced HTTPS scheme,
  callback at `/auth/google/callback`, plus App 1's `ProxyFix` so redirect URIs are
  correct behind Zeabur's reverse proxy. *(Adopted assumption — flag if you disagree.)*
- Unconfigured OAuth shows a helpful error page rather than a blank screen.

## 4. Architecture Overview (Option B)

A format-agnostic **core pipeline** does the shared work (tool check → device
registration → fulfillment → magic-byte validation → DRM removal). It then hands the
decrypted file to a **format handler** selected by the validated file type. Adding a
future format means adding one handler, not touching the core.

```
upload(.acsm)
   └─> Core pipeline ──> register ──> fulfill ──> VALIDATE MAGIC BYTES
                                                        │
                                          select handler by real type
                                                        │
                                   ┌────────────────────┴───────────────────┐
                                 EpubHandler                              PdfHandler
                                   │                                          │
                            remove DRM                                  remove DRM
                            cover via OPF/ZIP                            cover via PyMuPDF p.1
                            optional ZIP/OPF check                       optional text-presence check
                                   └──────────────> output + library <──────┘
```

## 5. Component Breakdown

### 5.1 libgourou wrapper (shared core)
- Tools, located in `libgourou/utils/` first, then `PATH`:
  - `adept_activate --anonymous --random-serial --output-dir $ADEPT_DIR`
  - `acsmdownloader -f <acsm> -o <out>`
  - `adept_remove -f <in> -o <out>`
- Device registration is **idempotent**: skip if `device.xml` + `activation.xml`
  already exist in `$ADEPT_DIR`.
- Timeouts: activation 60s, download 120s, DRM removal 60s; each surfaces a clear
  error on timeout or non-zero exit.

### 5.2 Format detection & validation
- **Pre-fulfillment (hint only):** parse the ACSM XML (`adept:src`, `adept:resource`,
  any `format` element) to *guess* EPUB vs PDF for UI display.
- **Post-fulfillment (authoritative):** sniff magic bytes of the downloaded file:
  - **PDF** → begins with `%PDF-`.
  - **EPUB** → ZIP container (`PK\x03\x04`) whose `mimetype` entry is `application/epub+zip`.
- The **validated** type chooses the handler. If the XML hint and real type disagree,
  the real type wins. If the file matches neither, the job fails cleanly (see §11).

### 5.3 Format handler contract
Each handler is a small, self-contained unit declaring:

| Responsibility | EpubHandler | PdfHandler |
|---|---|---|
| Format key / extension | `epub` / `.epub` | `pdf` / `.pdf` |
| Magic-byte signature it claims | ZIP + epub mimetype | `%PDF-` |
| Output filename pattern | `<stem>.epub` | `<stem>.pdf` |
| Cover extraction | OPF lookup → fallback name match in ZIP | render page 1 via PyMuPDF |
| Optional readability/integrity check (non-fatal) | valid ZIP + OPF present | page count + text-presence scan |

The core never branches on format internally — it only asks the matched handler to
perform these steps.

### 5.4 Job manager
- In-memory `active_jobs` dict guarded by a `threading.Lock`.
- One daemon thread per conversion; job record holds: `filename`, `status`
  (`running`/`done`/`error`), `steps[]`, `current_step`, `current_label`, `error`,
  `done_message`, `start_time`.
- `_prune_old_jobs` drops finished jobs older than ~2 hours.
- **Requires a single gunicorn worker** so the dict is shared (confirmed acceptable
  for single-user use).

### 5.5 Library & file management
- App 2's richer UI: grid/list toggle, search, sort (date/name/size), bulk select.
- **Deletion unified to by-stem**: deleting a book removes its output file(s), cover,
  and any leftover upload sharing that stem.
- Combined view shows both EPUB and PDF with distinct format badges.

### 5.6 Web routes (consolidated)
`/login`, `/login/google`, `/auth/google/callback`, `/logout`, `/` (converter),
`/library`, `/upload`, `/start-convert/<filename>`, `/job-status/<job_id>`,
`/download/<filename>`, `/delete/<stem>`, `/cover/<filename>`, `/debug-status`.
All non-auth routes require login; all filename inputs sanitized via basename only
(directory-traversal guard).

## 6. Conversion Pipeline (variable-length steps)

Canonical sequence emitted to the UI:

1. Check tools
2. Register Adobe device (skipped-fast if already registered)
3. Fulfill download
4. Validate file type (magic bytes) → select handler
5. Remove DRM
6. Post-process (cover extraction + optional non-fatal check)
→ `done`

The progress bar derives its maximum from the number of steps actually emitted,
rather than hardcoding 5 or 6. The optional check in step 6 never fails the job;
it only attaches an informational notice.

## 7. Filesystem Layout & Lifecycle

| Path | Volume? | Lifecycle |
|---|---|---|
| `/app/uploads` | No (ephemeral) | `.acsm` deleted after a successful conversion |
| `/app/output` | **Yes** | DRM-free files kept until manual delete |
| `/app/covers` | **Yes** | extracted covers, persist alongside output |
| `$ADEPT_DIR` | **Yes** | Adobe credentials; device registers once |

**Duplicate handling:** re-converting the same `stem` overwrites the existing output
(`<stem>.<ext>`). Flag if you'd prefer versioned filenames instead.

## 8. Deployment

- **Base image:** `python:3.11-slim`.
- **Build deps (libgourou only):** `git cmake make g++ libpugixml-dev libzip-dev
  libssl-dev libcurl4-openssl-dev`. **No OCR packages** → much smaller image, faster
  build, lower memory.
- **libgourou** built from source (`BUILD_UTILS=1 BUILD_STATIC=1 BUILD_SHARED=0`).
- **`$ADEPT_DIR` = `/app/.adept`** (canonical), set in the image **and** mounted as a
  volume at that *exact same path* in `zeabur.json`. (Fixes the current mismatch where
  the volume is `/root/.config/adept` but App 1 uses `/app/.adept`.)
- **Server:** gunicorn, **single worker**, `--threads 4`, `--timeout 300`,
  `--graceful-timeout 30` (no long timeout needed without OCR).
- **Port:** bind `$PORT` (Procfile) for Zeabur; `8080` fallback in Dockerfile CMD.
- **HEALTHCHECK:** `curl -f http://localhost:$PORT/login`.
- **Env vars:** `SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `ALLOWED_EMAIL`, `APP_BASE_URL`.

## 9. Dependencies

`flask`, `gunicorn`, `authlib`, `requests`, `PyMuPDF` (PDF covers + optional check),
`pypdf` (fallback PDF check). All Tesseract/Ghostscript/unpaper/pngquant removed.

## 10. Consolidated Decision Table

| # | Decision | Resolution |
|---|---|---|
| 1 | Format routing | Accept any `.acsm`; auto-route on **validated** type |
| 2 | Type authority | Magic-byte sniff of fulfilled file (XML is hint only) |
| 3 | OCR | **Removed entirely**, incl. toolchain |
| 4 | Readability check | Kept, **lightweight & non-fatal** (overridable) |
| 5 | Library UI | App 2's richer UI |
| 6 | Delete semantics | **By-stem** |
| 7 | Auth | Google OAuth, single email; App 2 flow + `ProxyFix` |
| 8 | ADEPT path | `/app/.adept`, mounted as volume, register once |
| 9 | Output retention | Keep until manual delete |
| 10 | Uploads/covers | Uploads ephemeral; covers persist |
| 11 | Concurrency | Single worker + threads; in-memory locked job dict |
| 12 | Architecture | **Option B** — pluggable format handlers |

## 11. Edge Cases & Error Handling

- **Unsupported fulfilled type** (neither EPUB nor PDF) → fail with clear message;
  do not add to library; clean the upload.
- **Expired/invalid token** → `acsmdownloader` non-zero exit surfaced to the user.
- **Adobe activation/fulfillment server unreachable** → timeout → explicit error.
- **DRM removal incomplete** (e.g., PDF still encrypted) → caught by the optional
  check and reported as a warning on the finished job.
- **Cover extraction failure** → non-fatal; placeholder shown.
- **Filename safety** → basename-only on every user-supplied filename/stem.
- **Concurrent jobs** → allowed (one thread each); no hard cap for a single user,
  but the job dict is lock-guarded.

## 12. Acceptance Criteria

- Logging in with the allowed email reaches the converter; any other email is denied.
- Uploading an EPUB-sourced `.acsm` yields a DRM-free `.epub`; a PDF-sourced one
  yields a DRM-free `.pdf` — without the user choosing a format.
- A token whose fulfilled file is neither type fails gracefully with a clear message.
- The Adobe device registers once and survives a container restart (persistent volume).
- Library shows both formats with covers, search/sort/bulk-delete, and by-stem deletion.
- The built image contains no OCR packages.

## 13. Resolved Decisions (final)

- **A.** Lightweight readability check — **kept** (non-fatal, informational).
- **B.** Duplicate stem — **overwrite** the existing output file.
- **C.** Canonical `$ADEPT_DIR` — **`/app/.adept`**.

---

*Spec locked. On your "You may start coding" I'll implement against this document.*
