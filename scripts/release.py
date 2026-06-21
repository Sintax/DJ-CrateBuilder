#!/usr/bin/env python3
"""DJ-CrateBuilder — one-command nightly release.

A single script that replaces three older files (``build-windows.bat``,
``scripts/release_nightly.py`` and ``scripts/init_nightly.py``). Run it from the
repo root with the GitHub CLI ``gh`` authenticated:

    python scripts/release.py             # bump+build+publish a *delta* nightly
    python scripts/release.py --help      # full help, flag reference, examples
    python scripts/release.py --dry-run   # build+zip locally; don't upload

What one run does, end to end:
  1. Auto-increment ``APP_BUILD`` in the source so the .exe reports the new build.
  2. Build the app (onedir) + ``updater.exe`` (onefile) + bundle FFmpeg.
  3. Work out the smallest payload: hash every file in the build, compare to the
     last full build's hashes, and zip ONLY the files that changed. The updater
     overlays them in place, so unchanged files (FFmpeg, the CPython runtime,
     updater.exe) are never re-downloaded. First run, or ``--full``, ships a full
     payload (minus the giant FFmpeg binaries) and becomes the new baseline.
  4. SHA-256 the zip, upload it to the reused `nightly` GitHub pre-release, and
     push ``update.json`` to the `nightly` branch via git plumbing — your current
     checkout and `main` are never touched.

The ONLY manual step left is compiling the Windows installer in Inno Setup, and
that's only needed when you cut a *fresh installer* — nightly updates swap files
in place and never need it.

Why a delta is safe: the updater (cratebuilder/updater_core.apply_update) is an
additive overlay — it copies the zip's files over the install and leaves every
other file alone. Deltas are diffed against a FIXED baseline (the last full
build), so a single delta zip always carries the complete current version of
every file that ever changed since that baseline. Applying it over any install
derived from that baseline yields the full current build, even if the user
skipped intervening nightlies.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

# ── Project constants ────────────────────────────────────────────────────────
REPO = "Sintax/DJ-CrateBuilder"
NIGHTLY_TAG = "nightly"
APP_NAME = "DJ-CrateBuilder"
DIST_DIR = os.path.join("dist", APP_NAME)        # PyInstaller onedir output
STATE_FILE = ".nightly_release_state.json"       # local baseline (gitignored)
# Big static binaries: present on every install from the installer, never change
# between code-only nightlies, so they're excluded from a full/fallback payload.
FFMPEG_BINS = ("ffmpeg.exe", "ffprobe.exe")

# This script lives at <repo>/scripts/release.py — REPO_ROOT walks up one parent
# (`scripts/` → repo root) so every path below resolves relative to the actual
# repo, no matter where the user invokes it from.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Small helpers ────────────────────────────────────────────────────────────
def run(cmd, **kw):
    """Run a subprocess, echoing it; raise SystemExit on failure."""
    print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        sys.exit(f"command failed ({r.returncode}): {' '.join(cmd)}")
    return r


def _git(*args, stdin=None, check=True):
    """Run a git command. stdin is piped as BYTES so Python never rewrites
    \\n -> \\r\\n on Windows — git plumbing (mktree) parses records strictly and a
    stray \\r becomes part of the filename (e.g. a tree entry "update.json\\r")."""
    input_bytes = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    r = subprocess.run(["git", *args], capture_output=True, input=input_bytes)
    if check and r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        sys.exit(f"git {' '.join(args)} failed:\n{err}")
    return r.stdout.decode("utf-8", "replace").strip()


def _sha256(path, chunk=65536):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _fmt_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{num} B"
        num /= 1024


# ── Source version / build wrangling ─────────────────────────────────────────
def find_source():
    """Locate the single DJ-CrateBuilder_vX.Y.py entry-point in the repo root."""
    matches = glob.glob(os.path.join(REPO_ROOT, "DJ-CrateBuilder_v*.py"))
    if len(matches) != 1:
        sys.exit(f"expected exactly one DJ-CrateBuilder_v*.py, found {matches}")
    return matches[0]


def read_version_build(src):
    text = open(src, encoding="utf-8").read()
    ver = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M)
    bld = re.search(r"^APP_BUILD\s*=\s*(\d+)", text, re.M)
    if not ver or not bld:
        sys.exit("couldn't find APP_VERSION / APP_BUILD in the source file")
    return ver.group(1), int(bld.group(1))


def write_build(src, new_build):
    """Rewrite the APP_BUILD line in place, preserving its alignment."""
    text = open(src, encoding="utf-8").read()
    new_text, n = re.subn(r"^(APP_BUILD\s*=\s*)\d+",
                          lambda m: f"{m.group(1)}{new_build}", text, count=1,
                          flags=re.M)
    if n != 1:
        sys.exit("failed to update APP_BUILD in the source file")
    with open(src, "w", encoding="utf-8") as f:
        f.write(new_text)


def write_readme_build(new_build):
    """Sync the ``(Build_N)`` marker in the README H1 with the new build number.

    Non-fatal: if the marker is missing (README renamed or reformatted) we warn
    and carry on — a cosmetic mismatch must never block a release."""
    path = os.path.join(REPO_ROOT, "README.md")
    if not os.path.exists(path):
        print("  [!] README.md not found — skipping build-number sync.")
        return
    text = open(path, encoding="utf-8").read()
    new_text, n = re.subn(r"\(Build_\d+\)", f"(Build_{new_build})", text, count=1)
    if n != 1:
        print("  [!] README '(Build_N)' marker not found — skipping sync.")
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"[+] README build marker -> (Build_{new_build})")


# ── Build step (replaces build-windows.bat) ──────────────────────────────────
def ensure_pyinstaller():
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  [*] PyInstaller not found — installing...")
        run([sys.executable, "-m", "pip", "install", "pyinstaller", "--quiet"])


def build_app(src):
    """Build the app onedir + updater.exe (onefile) and bundle FFmpeg."""
    ensure_pyinstaller()
    print("\n[*] Building the main app (onedir)...")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--name", APP_NAME, "--windowed", "--onedir", "--icon", "icon.ico",
         "--collect-submodules", "cratebuilder",
         "--hidden-import", "pystray._win32",
         "--hidden-import", "PIL.ImageDraw",
         "--hidden-import", "send2trash",
         src], cwd=REPO_ROOT)

    print("\n[*] Building updater.exe (onefile)...")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--name", "updater", "--windowed", "--onefile",
         "--hidden-import", "cratebuilder.updater_core",
         "updater.py"], cwd=REPO_ROOT)
    shutil.copy2(os.path.join(REPO_ROOT, "dist", "updater.exe"),
                 os.path.join(REPO_ROOT, DIST_DIR, "updater.exe"))
    print("  [+] updater.exe placed next to the app.")

    print("\n[*] Bundling FFmpeg...")
    dest = os.path.join(REPO_ROOT, DIST_DIR)
    copied = 0
    for name in ("ffmpeg", "ffprobe"):
        found = shutil.which(name)
        if found:
            shutil.copy2(found, os.path.join(dest, os.path.basename(found)))
            copied += 1
    if copied == 2:
        print("  [+] Copied ffmpeg.exe + ffprobe.exe from PATH.")
    else:
        print("  [!] FFmpeg not fully found on PATH. The published payload won't\n"
              "      include it — fine for nightly updates (users already have it\n"
              "      from the installer). For a fresh install, copy ffmpeg.exe and\n"
              "      ffprobe.exe into dist/DJ-CrateBuilder/ before Inno Setup.")


# ── Payload (delta vs full) ──────────────────────────────────────────────────
def hash_tree(root):
    """Map every file under root to its SHA-256, keyed by forward-slash relpath."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            ap = os.path.join(dirpath, name)
            rel = os.path.relpath(ap, root).replace(os.sep, "/")
            out[rel] = _sha256(ap)
    return out


def load_state():
    path = os.path.join(REPO_ROOT, STATE_FILE)
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(base_build, hashes):
    path = os.path.join(REPO_ROOT, STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"base_build": base_build, "files": hashes}, f, indent=2)


def choose_payload(dist_abs, build, force_full):
    """Return (rel_paths, is_full, base_build, new_hashes).

    Full  → every file except the FFmpeg binaries (they overlay from the existing
            install). Becomes the new baseline.
    Delta → only files whose hash differs from the baseline. FFmpeg never changes,
            so it drops out naturally. Baseline stays FIXED so deltas compose.
    """
    new_hashes = hash_tree(dist_abs)
    state = None if force_full else load_state()

    if state is None:
        rels = [r for r in new_hashes
                if os.path.basename(r).lower() not in FFMPEG_BINS]
        return sorted(rels), True, build, new_hashes

    base = state.get("files", {})
    base_build = int(state.get("base_build", build))
    rels = [r for r, h in new_hashes.items() if base.get(r) != h]
    return sorted(rels), False, base_build, new_hashes


def make_zip(dist_abs, rels, zip_path):
    """Zip the chosen files at their dist-relative paths (so they sit at the zip
    root exactly where the updater overlays them)."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in rels:
            zf.write(os.path.join(dist_abs, rel.replace("/", os.sep)), rel)
    return zip_path


# ── GitHub upload + manifest publish (replaces release_nightly.py) ───────────
def gh_upload(zip_path, repo, tag):
    subprocess.run(
        ["gh", "release", "create", tag, "--repo", repo, "--prerelease",
         "--title", "Nightly builds",
         "--notes", "Rolling nightly channel. Do not use as a stable release."],
        capture_output=True, text=True)  # ignore "already exists"
    res = subprocess.run(
        ["gh", "release", "upload", tag, zip_path, "--repo", repo, "--clobber"],
        capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"gh upload failed:\n{res.stderr}")
    print(f"  [+] uploaded asset to {repo} release '{tag}'.")
    prune_old_zip_assets(repo, tag, keep=os.path.basename(zip_path))


def prune_old_zip_assets(repo, tag, keep):
    """Keep only the freshly uploaded ``.zip`` on the nightly release and delete
    older payloads, so the Assets list doesn't grow without bound. Each manifest
    references exactly one zip (the latest), so older zips are never downloaded.

    Non-fatal: pruning failures warn but never abort an otherwise-good publish."""
    res = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo,
         "--json", "assets", "--jq", ".assets[].name"],
        capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  [!] couldn't list assets to prune: {res.stderr.strip()}")
        return
    stale = [n for n in res.stdout.splitlines()
             if n.strip() and n.endswith(".zip") and n != keep]
    for name in stale:
        d = subprocess.run(
            ["gh", "release", "delete-asset", tag, name, "--repo", repo, "--yes"],
            capture_output=True, text=True)
        if d.returncode == 0:
            print(f"  [+] pruned old nightly asset {name}.")
        else:
            print(f"  [!] failed to prune {name}: {d.stderr.strip()}")


def publish_manifest(manifest_text, build, branch="nightly"):
    """Commit update.json to the nightly branch via plumbing — no working-tree
    change, no branch switch."""
    # Fetch into refs/remotes/origin/<branch> and resolve via that ref (NOT
    # FETCH_HEAD): `gh release create nightly` also makes a tag named `nightly`,
    # and FETCH_HEAD would resolve to the tag, making the push non-fast-forward.
    _git("fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}")
    try:
        parent = _git("rev-parse", f"refs/remotes/origin/{branch}")
    except SystemExit:
        sys.exit(f"Couldn't find origin/{branch}. Run: python scripts/release.py --init")
    blob = _git("hash-object", "-w", "--stdin", stdin=manifest_text)
    tree = _git("mktree", stdin=f"100644 blob {blob}\tupdate.json\n")
    commit = _git("commit-tree", tree, "-p", parent, "-m", f"nightly: build {build}")
    _git("push", "origin", f"{commit}:refs/heads/{branch}")
    print(f"  [+] published manifest to origin/{branch} (commit {commit[:9]}).")


def cleanup_artifacts(zip_paths=None):
    """Delete the local build leftovers (build/, dist/, and DJ-CrateBuilder-*.zip).

    Safe to remove: the delta baseline lives in STATE_FILE (file hashes), not in
    dist/, so the next run rebuilds dist/ fresh and still diffs correctly. A
    published zip already lives on the GitHub release, so the local copy is junk.

    Pass ``zip_paths`` (a list) to scope zip deletion to specific files (used
    right after a publish). Omit it to sweep every DJ-CrateBuilder-*.zip in the
    repo root — that's what ``--cleanup`` uses to tidy up after a ``--dry-run``.
    """
    removed = []
    for folder in ("build", "dist"):
        p = os.path.join(REPO_ROOT, folder)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            removed.append(folder + "/")
    targets = zip_paths if zip_paths is not None else glob.glob(
        os.path.join(REPO_ROOT, f"{APP_NAME}-*.zip"))
    for zp in targets:
        if os.path.exists(zp):
            try:
                os.remove(zp)
                removed.append(os.path.basename(zp))
            except OSError:
                pass
    if removed:
        print(f"[+] Cleaned up local artifacts: {', '.join(removed)}")
    else:
        print("[+] Nothing to clean up.")


def init_nightly():
    """One-time: create the orphan `nightly` branch holding only update.json."""
    _git("rev-parse", "--git-dir")
    if _git("branch", "--list", "nightly"):
        sys.exit("A local `nightly` branch already exists — nothing to do.")
    remote = subprocess.run(["git", "ls-remote", "--heads", "origin", "nightly"],
                            capture_output=True, text=True).stdout.strip()
    if remote:
        sys.exit("origin/nightly already exists — fetch it instead of re-creating.")
    manifest = ('{\n  "version": "1.3",\n  "build": 0,\n  "url": "",\n'
                '  "sha256": "%s",\n'
                '  "notes": "Initial nightly manifest. release.py overwrites this."\n}\n'
                % ("0" * 64))
    blob = _git("hash-object", "-w", "--stdin", stdin=manifest)
    tree = _git("mktree", stdin=f"100644 blob {blob}\tupdate.json\n")
    commit = _git("commit-tree", tree, "-m", "nightly channel: initial manifest")
    _git("branch", "nightly", commit)
    _git("push", "origin", "refs/heads/nightly:refs/heads/nightly")
    _git("branch", "--set-upstream-to=origin/nightly", "nightly")
    print(f"Created and pushed origin/nightly (commit {commit[:9]}). Channel is live.")


# ── Main ─────────────────────────────────────────────────────────────────────
HELP_DESCRIPTION = """\
DJ-CrateBuilder — one-command nightly release.

A single script that bumps the build number, builds the app (+ updater +
FFmpeg), zips ONLY the files that changed since the last full build (the delta),
uploads the zip to the `nightly` GitHub pre-release, and pushes update.json to
the `nightly` branch. The in-app updater then serves it to users automatically.

Run from anywhere; the script always resolves paths to the repo root.
Requires the GitHub CLI `gh` to be authenticated.
"""

HELP_EPILOG = """\
typical use:
  python scripts/release.py
      The normal nightly. Prompts for one line of notes, auto-bumps APP_BUILD,
      builds, ships a delta payload, uploads, and cleans up build/+dist/+zip.

  python scripts/release.py --notes "Fixed Watch List crash."
      Same as above but skip the notes prompt.

  python scripts/release.py --dry-run
      Rehearsal: bump, build, and zip locally; print the manifest; DO NOT
      upload or publish, and KEEP the artifacts so you can inspect them.

  python scripts/release.py --full --notes "Bumped Python runtime."
      Force a full payload and reset the delta baseline. Use after any change
      that touches the bundled CPython runtime, dependencies, or updater.exe
      (otherwise users on older builds won't get the new runtime files).

  python scripts/release.py --build-only
      Just build dist/ for a fresh installer — no bump, no upload, no publish,
      no cleanup. Smoke-test dist/, then compile in Inno Setup.

  python scripts/release.py --init
      One-time: create and push the orphan `nightly` branch (already done for
      v1.3 — only needed on a fresh clone or if the branch is deleted).

  python scripts/release.py --cleanup
      Tidy up: delete build/, dist/, and every DJ-CrateBuilder-*.zip in the
      repo root, then exit. Handy after a --dry-run, which keeps artifacts.

behavior notes:
  - Full payload     auto-excludes ffmpeg.exe/ffprobe.exe (they're static and
                     already on every install from the installer).
  - Delta payload    diffs against a FIXED baseline (the last `--full` build,
                     stored in .nightly_release_state.json). Users who skip
                     nightlies still converge on the correct current build.
  - Cleanup          after a successful publish, build/, dist/, and the zip
                     are deleted automatically. Pass --keep to retain them.
  - Source bump      if the build step fails, the APP_BUILD bump is reverted
                     so the next run doesn't skip a build number.
  - Safety           your working tree and `main` branch are NEVER touched.
                     Manifest publish uses git plumbing on the `nightly` ref.
"""


def main(argv=None):
    # Windows consoles default to cp1252, which can't encode the arrows/box
    # characters in our status lines (e.g. the "→" in the --build-only message),
    # so a trailing print would crash with UnicodeEncodeError *after* a fully
    # successful build. Force UTF-8 on the output streams up front.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        prog="release.py",
        description=HELP_DESCRIPTION,
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    publish = ap.add_argument_group("publish flags (the normal nightly path)")
    publish.add_argument("--notes", default=None, metavar="TEXT",
                         help="release notes for this build (prompted if omitted)")
    publish.add_argument("--build", type=int, default=None, metavar="N",
                         help="override the auto-incremented build number")
    publish.add_argument("--full", action="store_true",
                         help="ship a full payload (minus FFmpeg) and reset the "
                              "delta baseline — use after runtime/dep changes")
    publish.add_argument("--keep", action="store_true",
                         help="keep build/, dist/ and the zip after a successful "
                              "publish (they're deleted by default)")

    alt = ap.add_argument_group("alternate modes (don't publish)")
    alt.add_argument("--dry-run", action="store_true",
                     help="bump+build+zip locally and print the manifest; do NOT "
                          "upload or publish; artifacts are NOT cleaned up")
    alt.add_argument("--no-build", action="store_true",
                     help="skip PyInstaller; publish from the existing dist/")
    alt.add_argument("--build-only", action="store_true",
                     help="just build dist/ for a fresh installer (no bump, no "
                          "publish, no cleanup) — for the Inno Setup path")
    alt.add_argument("--init", action="store_true",
                     help="one-time: create the `nightly` branch, then exit")
    alt.add_argument("--cleanup", action="store_true",
                     help="delete build/, dist/, and every DJ-CrateBuilder-*.zip "
                          "in the repo root, then exit (handy after --dry-run)")

    args = ap.parse_args(argv)
    os.chdir(REPO_ROOT)

    if args.init:
        init_nightly()
        return 0

    if args.cleanup:
        cleanup_artifacts()
        return 0

    src = find_source()
    version, current_build = read_version_build(src)

    # Fresh-installer build path: build dist/ as-is (no bump, no publish), then
    # hand off to a smoke test + Inno Setup.
    if args.build_only:
        build_app(src)
        print(f"\nBuilt dist/{APP_NAME}/  (v{version}.{current_build}).")
        print("Next: smoke-test dist/DJ-CrateBuilder/DJ-CrateBuilder.exe, then "
              "compile the installer in Inno Setup (Packaging_Guide.md → Step 4).")
        return 0

    new_build = args.build if args.build is not None else current_build + 1

    notes = args.notes
    if notes is None and not args.dry_run:
        notes = input("Release notes (one line, what changed): ").strip()
    notes = notes or ""

    print(f"\n=== DJ-CrateBuilder nightly  v{version}.{new_build} ===")

    # 1. Bump the source so the frozen .exe reports the new build, and sync the
    #    README's (Build_N) marker so the repo's front page matches the channel.
    original_src = open(src, encoding="utf-8").read()
    readme_path = os.path.join(REPO_ROOT, "README.md")
    original_readme = (open(readme_path, encoding="utf-8").read()
                       if os.path.exists(readme_path) else None)
    if new_build != current_build:
        write_build(src, new_build)
        print(f"[+] APP_BUILD: {current_build} -> {new_build}")
        write_readme_build(new_build)

    # 2. Build (restore the source + README bumps if the build fails, so re-runs
    #    don't skip a build number).
    dist_abs = os.path.join(REPO_ROOT, DIST_DIR)
    if not args.no_build:
        try:
            build_app(src)
        except SystemExit:
            with open(src, "w", encoding="utf-8") as f:
                f.write(original_src)
            if original_readme is not None:
                with open(readme_path, "w", encoding="utf-8") as f:
                    f.write(original_readme)
            print("[!] build failed — reverted the APP_BUILD bump.")
            raise
    if not os.path.isdir(dist_abs):
        sys.exit(f"build folder not found: {dist_abs} (drop --no-build to build it)")

    # 3. Decide payload and zip it.
    rels, is_full, base_build, new_hashes = choose_payload(dist_abs, new_build, args.full)
    if not rels:
        sys.exit("no file changes vs the baseline — nothing to publish.")
    zip_name = f"{APP_NAME}-{version}.{new_build}.zip"
    zip_path = os.path.join(REPO_ROOT, zip_name)
    make_zip(dist_abs, rels, zip_path)
    digest = _sha256(zip_path)
    kind = "FULL (new baseline)" if is_full else f"DELTA vs build {base_build}"
    print(f"\n[+] Payload: {kind} — {len(rels)} file(s), "
          f"{_fmt_size(os.path.getsize(zip_path))}")
    print(f"    sha256 {digest}")

    url = f"https://github.com/{REPO}/releases/download/{NIGHTLY_TAG}/{zip_name}"
    manifest = {
        "version": version, "build": new_build, "url": url,
        "sha256": digest, "notes": notes, "base": base_build,
    }
    manifest_text = json.dumps(manifest, indent=2) + "\n"

    if args.dry_run:
        print("\n[dry-run] Skipping upload + publish. Manifest would be:")
        print(manifest_text)
        return 0

    # 4. Upload + publish.
    gh_upload(zip_path, REPO, NIGHTLY_TAG)
    publish_manifest(manifest_text, new_build)

    # Re-baseline only on a full publish; deltas keep the baseline FIXED so they
    # stay correct for users who skipped builds.
    if is_full:
        save_state(new_build, new_hashes)
        print(f"[+] Recorded build {new_build} as the new baseline.")

    # Tidy up the local build leftovers now that the upload succeeded.
    if not args.keep:
        cleanup_artifacts([zip_path])

    print(f"\nDone — v{version}.{new_build} is live on the nightly channel.")
    print(f"Remember to commit the APP_BUILD bump in {os.path.basename(src)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
