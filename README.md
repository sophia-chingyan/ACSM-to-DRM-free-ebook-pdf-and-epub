# ACSM Converter (unified EPUB + PDF)

A personal-use web app that turns an Adobe `.acsm` token into a **DRM-free EPUB or
PDF**, auto-detecting which one the token actually fulfils. It merges the previous
EPUB-only and PDF-only tools into a single app built around pluggable format handlers.

## How it works

1. You upload an `.acsm` file.
2. The app registers an anonymous Adobe device (once) via **libgourou**.
3. It fulfils the download, then **validates the real file type by magic bytes**
   (`%PDF-` for PDF; a ZIP with an `application/epub+zip` mimetype for EPUB).
4. It removes DRM (decryption only — no re-encoding; all images, fonts, links,
   bookmarks, and structure are preserved).
5. It extracts a cover and runs a quick, non-fatal readability check, then the file
   appears in your library.

There is **no OCR** — image-only PDFs are kept as-is.

## Authentication

Google OAuth2; only the address in `ALLOWED_EMAIL` can log in. Single user.

## Environment variables (set in Zeabur)

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | yes | Flask session secret (fixed value keeps sessions across restarts) |
| `GOOGLE_CLIENT_ID` | yes | From Google Cloud Console → Credentials |
| `GOOGLE_CLIENT_SECRET` | yes | From Google Cloud Console → Credentials |
| `ALLOWED_EMAIL` | yes | The one Google email allowed to log in |
| `APP_BASE_URL` | no | e.g. `https://your-app.zeabur.app`; used to build the OAuth redirect URI |

## Google OAuth2 setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create a project → APIs & Services → Credentials → OAuth 2.0 Client ID.
3. Add the authorised redirect URI: `https://YOUR-DOMAIN.zeabur.app/auth/google/callback`.
4. Copy the Client ID and Secret into the Zeabur variables above.

## Persistent storage (Zeabur volumes)

| Volume | Path | Purpose |
|---|---|---|
| `ebook-output` | `/app/output` | Converted files — **kept until you delete them** |
| `ebook-covers` | `/app/covers` | Extracted cover images |
| `adobe-device` | `/app/.adept` | Adobe credentials — registered once, reused |

Uploaded `.acsm` files are single-use and are removed after a successful conversion.

## Running locally

```
pip install -r requirements.txt
# libgourou tools must be on PATH or in ./libgourou/utils/
python app.py            # serves on http://localhost:8080
```

OAuth requires the Google variables above; without them the login page shows a
configuration notice.

## Notes & limits

- Single gunicorn worker (the job tracker is in-memory); fine for one user.
- The `.acsm` token is one-time-use; re-converting the same title overwrites the
  existing output file.
- If a token fulfils to neither EPUB nor PDF, the job fails with a clear message
  and nothing is added to the library.
