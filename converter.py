#!/usr/bin/env python3
"""
Unified ACSM Converter — core pipeline + pluggable format handlers.

Accepts an Adobe ``.acsm`` token, registers an anonymous Adobe device,
fulfils the download via libgourou, validates the *actual* file type by
magic bytes, removes DRM, and post-processes (cover + a lightweight,
non-fatal readability check) through a per-format handler.

Architecture (spec Option B):
    A format-agnostic core does the shared work:
        check tools -> register device -> fulfil -> VALIDATE MAGIC BYTES
        -> remove DRM
    then hands the decrypted file to the handler matched by the validated
    type (EpubHandler / PdfHandler) for cover extraction and verification.

DRM removal is decryption-only: no re-encoding or transcoding. Images,
fonts, CSS (incl. vertical-writing CJK), links, bookmarks, and paragraph
structure are preserved exactly.

Prerequisites (the Docker image provides these):
    libgourou (built from source), PyMuPDF, pypdf
"""

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIBGOUROU_DIR = SCRIPT_DIR / "libgourou"
LIBGOUROU_BIN = LIBGOUROU_DIR / "utils"

# Canonical ADEPT credential directory (spec decision #8). libgourou v0.8.1+
# reads $ADEPT_DIR; we set it explicitly so every tool shares one path. In the
# container this is /app/.adept and is mounted as a persistent volume so the
# device registers only once and survives restarts.
ADEPT_DIR = Path(os.environ.get("ADEPT_DIR", str(SCRIPT_DIR / ".adept")))

SUPPORTED_EXTENSIONS = {".epub", ".pdf"}

TOTAL_STEPS = 6
STEP_LABELS = {
    1: "Checking tools...",
    2: "Registering Adobe device...",
    3: "Downloading ebook...",
    4: "Validating file type...",
    5: "Removing DRM...",
    6: "Finishing up...",
}


# ─── libgourou plumbing ────────────────────────────────────────────────────


def _set_adept_env():
    """Ensure $ADEPT_DIR is set and present for all libgourou subprocesses."""
    ADEPT_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["ADEPT_DIR"] = str(ADEPT_DIR)


def run(cmd, **kwargs):
    """Run a command, returning the CompletedProcess."""
    _set_adept_env()
    defaults = {"capture_output": True, "text": True}
    defaults.update(kwargs)
    return subprocess.run(cmd, **defaults)


def find_tool(name):
    """Locate a libgourou tool: local build dir first, then PATH."""
    local = LIBGOUROU_BIN / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


# ─── Format detection & validation ──────────────────────────────────────────


def detect_format_hint(acsm_path):
    """Parse the ACSM XML for a *hint* of EPUB vs PDF (UI only, not authoritative).

    Returns 'epub', 'pdf', or None.
    """
    try:
        root = ET.parse(acsm_path).getroot()
    except ET.ParseError:
        return None
    ns = {"adept": "http://ns.adobe.com/adept"}

    for xpath in (".//adept:src", ".//adept:resource"):
        elem = root.find(xpath, ns)
        if elem is not None and elem.text:
            text = elem.text.lower()
            if ".pdf" in text or "output=pdf" in text:
                return "pdf"
            if ".epub" in text or "output=epub" in text:
                return "epub"

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "format" and elem.text:
            low = elem.text.lower()
            if "pdf" in low:
                return "pdf"
            if "epub" in low:
                return "epub"
    return None


def validate_file_type(path):
    """Authoritatively determine the file type by magic bytes (spec decision #2).

    Returns 'pdf', 'epub', or None if the file is neither.
    """
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None

    if head.startswith(b"%PDF-"):
        return "pdf"

    # EPUB is a ZIP container; confirm via the mimetype entry, with a
    # fallback to the presence of an OPF package document.
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                try:
                    mimetype = zf.read("mimetype").decode("ascii", "replace").strip()
                except KeyError:
                    mimetype = ""
                if mimetype == "application/epub+zip":
                    return "epub"
                if any(name.lower().endswith(".opf") for name in zf.namelist()):
                    return "epub"
        except zipfile.BadZipFile:
            return None
    return None


# ─── Format handlers (Option B) ─────────────────────────────────────────────


class FormatHandler:
    """Per-format post-processing strategy.

    Subclasses declare the format key/extension and implement cover
    extraction and a lightweight, non-fatal verification.
    """

    key = ""
    extension = ""

    def output_filename(self, stem):
        return f"{stem}.{self.extension}"

    def extract_cover(self, file_path, cover_dir):
        """Return the cover filename written into ``cover_dir``, or None."""
        raise NotImplementedError

    def verify(self, file_path):
        """Return (warning: bool, message: str). Never raises for content issues."""
        raise NotImplementedError


class EpubHandler(FormatHandler):
    key = "epub"
    extension = "epub"

    def extract_cover(self, file_path, cover_dir):
        file_path = Path(file_path)
        cover_dir = Path(cover_dir)
        cover_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                cover_name = self._find_cover_in_opf(zf) or self._find_cover_by_name(zf)
                if not cover_name:
                    return None
                data = zf.read(cover_name)
                ext = Path(cover_name).suffix or ".jpg"
                out = cover_dir / f"{file_path.stem}{ext}"
                out.write_bytes(data)
                return out.name
        except Exception:
            return None

    @staticmethod
    def _find_cover_in_opf(zf):
        opf_path = next((n for n in zf.namelist() if n.lower().endswith(".opf")), None)
        if not opf_path:
            return None
        try:
            root = ET.fromstring(zf.read(opf_path).decode("utf-8", "replace"))
        except ET.ParseError:
            return None
        opf_dir = str(Path(opf_path).parent)

        def resolve(href):
            return href if opf_dir == "." else f"{opf_dir}/{href}"

        cover_id = None
        for meta in root.iter():
            if meta.tag.endswith("}meta") or meta.tag == "meta":
                if meta.get("name") == "cover":
                    cover_id = meta.get("content")
                    break

        for item in root.iter():
            if item.tag.endswith("}item") or item.tag == "item":
                if cover_id and item.get("id") == cover_id and item.get("href"):
                    return resolve(item.get("href"))
                if not cover_id and "cover-image" in (item.get("properties") or ""):
                    if item.get("href"):
                        return resolve(item.get("href"))
        return None

    @staticmethod
    def _find_cover_by_name(zf):
        for name in zf.namelist():
            low = name.lower()
            if "cover" in low and low.endswith((".jpg", ".jpeg", ".png")):
                return name
        return None

    def verify(self, file_path):
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    return True, f"EPUB archive may be corrupt (bad entry: {bad})."
                has_opf = any(n.lower().endswith(".opf") for n in zf.namelist())
                if not has_opf:
                    return True, "EPUB has no OPF package document — file may be malformed."
                return False, "EPUB verified: valid archive with package document."
        except zipfile.BadZipFile:
            return True, "Output is not a valid EPUB (ZIP) archive."
        except Exception as exc:
            return False, f"EPUB check skipped: {exc}"


class PdfHandler(FormatHandler):
    key = "pdf"
    extension = "pdf"

    def extract_cover(self, file_path, cover_dir):
        file_path = Path(file_path)
        cover_dir = Path(cover_dir)
        cover_dir.mkdir(parents=True, exist_ok=True)
        out = cover_dir / f"{file_path.stem}.jpg"
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return None
        try:
            doc = fitz.open(str(file_path))
            try:
                if doc.page_count == 0:
                    return None
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                pix.save(str(out))
                return out.name
            finally:
                doc.close()
        except Exception:
            return None

    def verify(self, file_path):
        result = self._check_with_pymupdf(file_path)
        if result is not None:
            return result
        result = self._check_with_pypdf(file_path)
        if result is not None:
            return result
        return False, "PDF check skipped (no PDF library available)."

    @staticmethod
    def _check_with_pymupdf(file_path):
        try:
            import fitz
        except ImportError:
            return None
        try:
            doc = fitz.open(str(file_path))
        except Exception as exc:
            return True, f"PDF could not be opened: {exc}"
        try:
            if doc.is_encrypted:
                return True, "PDF is still encrypted — DRM removal may have failed."
            total = doc.page_count
            with_text = sum(
                1 for page in doc if (page.get_text("text") or "").strip()
            )
            if total == 0:
                return True, "PDF has no pages."
            if with_text == 0:
                return False, (
                    f"PDF verified: {total} page(s). No extractable text "
                    "(image-only scan) — file is fine for reading."
                )
            return False, (
                f"PDF verified: {with_text}/{total} page(s) have selectable text."
            )
        finally:
            doc.close()

    @staticmethod
    def _check_with_pypdf(file_path):
        try:
            from pypdf import PdfReader
        except ImportError:
            return None
        try:
            reader = PdfReader(str(file_path))
        except Exception as exc:
            return True, f"PDF could not be opened: {exc}"
        if reader.is_encrypted:
            return True, "PDF is still encrypted — DRM removal may have failed."
        total = len(reader.pages)
        if total == 0:
            return True, "PDF has no pages."
        return False, f"PDF verified: {total} page(s)."


HANDLERS = {h.key: h for h in (EpubHandler(), PdfHandler())}
_EXT_TO_KEY = {f".{h.extension}": h.key for h in HANDLERS.values()}


def get_handler_for_suffix(path):
    """Return the handler matching a file's extension, or None."""
    return HANDLERS.get(_EXT_TO_KEY.get(Path(path).suffix.lower()))


def extract_cover(file_path, cover_dir):
    """Dispatch cover extraction to the handler for this file's type."""
    handler = get_handler_for_suffix(file_path)
    if handler is None:
        return None
    return handler.extract_cover(file_path, cover_dir)


# ─── Core pipeline steps ─────────────────────────────────────────────────────


def register_device():
    """Register an anonymous Adobe device once. Idempotent."""
    device_file = ADEPT_DIR / "device.xml"
    activation_file = ADEPT_DIR / "activation.xml"
    if device_file.exists() and activation_file.exists():
        print("[OK] Adobe device already registered.", flush=True)
        return

    tool = find_tool("adept_activate")
    if not tool:
        raise RuntimeError("adept_activate not found (libgourou not built).")

    ADEPT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [tool, "--anonymous", "--random-serial", "--output-dir", str(ADEPT_DIR)]
    print(f"[DEBUG] Running: {' '.join(cmd)} (ADEPT_DIR={ADEPT_DIR})", flush=True)

    try:
        result = run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Device registration timed out (60s). Adobe's activation server "
            "may be unreachable from this host."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"Device registration failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )

    # Some builds write to ~/.config/adept instead of --output-dir; reconcile.
    if not device_file.exists():
        home_adept = Path.home() / ".config" / "adept"
        if (home_adept / "device.xml").exists():
            for item in home_adept.iterdir():
                shutil.copy2(item, ADEPT_DIR / item.name)
        else:
            raise RuntimeError(
                "Device registration reported success but device.xml was not "
                "created in any expected location."
            )
    print("[OK] Adobe device registered.", flush=True)


def fulfill_acsm(acsm_path, output_path):
    """Fulfil the ACSM token, downloading the still-DRM'd file."""
    tool = find_tool("acsmdownloader")
    if not tool:
        raise RuntimeError("acsmdownloader not found (libgourou not built).")

    cmd = [tool, "-f", str(acsm_path), "-o", str(output_path)]
    try:
        result = run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Download timed out (120s). The ACSM token may be expired, or the "
            "fulfilment server is unreachable."
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"ACSM download failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:500]}"
        )
    if not Path(output_path).exists():
        raise RuntimeError("Download completed but the output file was not found.")


def remove_drm(input_path, output_path):
    """Decrypt the fulfilled file (container-level; preserves all content)."""
    tool = find_tool("adept_remove")
    if not tool:
        raise RuntimeError("adept_remove not found (libgourou not built).")

    cmd = [tool, "-f", str(input_path), "-o", str(output_path)]
    try:
        result = run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("DRM removal timed out (60s).")
    if result.returncode != 0:
        raise RuntimeError(
            f"DRM removal failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )


# ─── Pipeline orchestrator ───────────────────────────────────────────────────


def convert_pipeline(acsm_path, output_dir, cover_dir=None):
    """Generator yielding (step:int|'done', message:str, warning:bool).

    Raises RuntimeError on any fatal failure. The verification at step 6 is
    non-fatal: it only attaches an informational/warning message.
    """
    acsm_path = Path(acsm_path).resolve()
    if not acsm_path.exists():
        raise RuntimeError(f"File not found: {acsm_path}")
    if acsm_path.suffix.lower() != ".acsm":
        raise RuntimeError(f"Not an ACSM file: {acsm_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = acsm_path.stem

    # Step 1 — tools
    missing = [
        name
        for name in ("acsmdownloader", "adept_activate", "adept_remove")
        if not find_tool(name)
    ]
    if missing:
        raise RuntimeError(
            "Missing libgourou tools: " + ", ".join(missing)
        )
    yield (1, "All tools ready.", False)

    # Step 2 — device
    register_device()
    yield (2, "Adobe device registered.", False)

    # Step 3 — fulfil (type still unknown; neutral temp name)
    drm_file = output_dir / f"{stem}.acsmdownload"
    try:
        fulfill_acsm(acsm_path, drm_file)
        size_kb = drm_file.stat().st_size / 1024
        yield (3, f"Downloaded ({size_kb:.0f} KB).", False)

        # Step 4 — authoritative type validation
        ftype = validate_file_type(drm_file)
        if ftype not in HANDLERS:
            raise RuntimeError(
                "Fulfilled file is neither a valid EPUB nor PDF; cannot continue. "
                "This ACSM may point to an unsupported format."
            )
        handler = HANDLERS[ftype]
        yield (4, f"Validated file type: {ftype.upper()}.", False)

        # Step 5 — DRM removal
        clean_file = output_dir / handler.output_filename(stem)
        remove_drm(drm_file, clean_file)
        yield (5, f"DRM removed: {clean_file.name}", False)
    finally:
        # The intermediate DRM'd download is never kept.
        try:
            drm_file.unlink(missing_ok=True)
        except OSError:
            pass

    # Step 6 — cover + lightweight, non-fatal verification
    if cover_dir is not None:
        try:
            handler.extract_cover(clean_file, cover_dir)
        except Exception:
            pass  # cover failure is non-fatal
    warning, message = handler.verify(clean_file)
    yield (6, message, warning)

    size_mb = clean_file.stat().st_size / (1024 * 1024) if clean_file.exists() else 0
    yield ("done", f"{clean_file.name}|{size_mb:.1f} MB", False)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def do_convert(acsm_file, output_dir):
    try:
        for step, message, warning in convert_pipeline(acsm_file, output_dir):
            if step == "done":
                name, _, size = message.partition("|")
                print(f"\n=== Done! ===\nFile: {name} ({size})")
            else:
                tag = "WARN" if warning else "OK"
                print(f"=== Step {step}/{TOTAL_STEPS} [{tag}]: {message} ===")
    except RuntimeError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Convert an ACSM token to a DRM-free EPUB or PDF."
    )
    parser.add_argument("acsm_file", nargs="?", help="Path to the .acsm file")
    parser.add_argument("-o", "--output-dir", default="output", help="Output directory")
    args = parser.parse_args()

    if not args.acsm_file:
        parser.print_help()
        sys.exit(1)
    do_convert(args.acsm_file, args.output_dir)


if __name__ == "__main__":
    main()
