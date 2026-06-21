"""Publish a nightly build of DJ-CrateBuilder.

Zips a PyInstaller onedir folder, computes its SHA-256, uploads it as an asset
on the GitHub `nightly` pre-release, and writes the `update.json` manifest the
running app reads. The display version stays pinned at 1.3 — only the build
number moves — so `main` and the tagged v1.3 release are never touched.

The zip is created so its *root contains the app folder's files directly*
(DJ-CrateBuilder.exe, updater.exe, the DLLs, ffmpeg, …). That matches what the
updater expects: it copies the zip's contents straight into the install folder.

Typical use (from the repo root, with the GitHub CLI `gh` authenticated):

    python scripts/release_nightly.py \
        --dist dist/DJ-CrateBuilder \
        --build 2 \
        --notes "Fixed the Watch List rescan crash on the installed build."

Steps it performs:
  1. Zip <dist> -> DJ-CrateBuilder-1.3.<build>.zip
  2. sha256 the zip
  3. gh release upload nightly <zip> --clobber   (unless --no-upload)
  4. Write update.json (next to this script's output, repo root by default)

Then YOU commit update.json to the `nightly` branch:

    git checkout nightly
    cp update.json .            # if you generated it elsewhere
    git add update.json && git commit -m "nightly: build 2" && git push

The `nightly` branch should contain ONLY update.json — keep it isolated from
main. Create it once as an orphan branch:

    git checkout --orphan nightly
    git rm -rf .
    cp scripts/update.json.example update.json   # then edit
    git add update.json && git commit -m "nightly channel: initial manifest"
    git push -u origin nightly
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile

APP_VERSION = "1.3"
REPO = "Sintax/DJ-CrateBuilder"
NIGHTLY_TAG = "nightly"


def _sha256(path, chunk=65536):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def zip_dist(dist_dir, zip_path):
    """Zip the *contents* of dist_dir so they sit at the zip root."""
    dist_dir = os.path.abspath(dist_dir)
    if not os.path.isdir(dist_dir):
        sys.exit(f"error: --dist folder not found: {dist_dir}")
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(dist_dir):
            for name in files:
                ap = os.path.join(root, name)
                arc = os.path.relpath(ap, dist_dir)   # root-relative -> zip root
                zf.write(ap, arc)
                count += 1
    print(f"  zipped {count} files -> {zip_path}")
    return zip_path


def _git(*args, stdin=None, check=True):
    # Pipe stdin as bytes (NOT text mode) so Python doesn't translate \n -> \r\n
    # on Windows. Git plumbing commands like `mktree` parse tab-/newline-
    # delimited records strictly: a stray \r becomes part of the filename and
    # silently produces a tree like "update.json\r".
    input_bytes = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    r = subprocess.run(["git", *args], capture_output=True, input=input_bytes)
    if check and r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        sys.exit(f"git {' '.join(args)} failed:\n{err}")
    return r.stdout.decode("utf-8", "replace").strip()


def publish_manifest(manifest_text, build, branch="nightly"):
    """Commit update.json to the nightly branch WITHOUT touching the working tree.

    Uses git plumbing: fetch the current branch tip, build a one-file tree with
    the new manifest, commit it on top, and push the ref. Your checkout and
    current branch are never switched or modified.
    """
    # Fetch the branch explicitly into refs/remotes/origin/<branch> and resolve
    # via that ref — NOT FETCH_HEAD. The `gh release create nightly` step also
    # creates a git tag called `nightly`, and FETCH_HEAD silently resolves to
    # the tag instead of the branch when both names collide, which makes the
    # subsequent push non-fast-forward.
    _git("fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}")
    try:
        parent = _git("rev-parse", f"refs/remotes/origin/{branch}")
    except SystemExit:
        sys.exit(f"Couldn't find origin/{branch}. Run init_nightly.py first.")
    blob = _git("hash-object", "-w", "--stdin", stdin=manifest_text)
    tree = _git("mktree", stdin=f"100644 blob {blob}\tupdate.json\n")
    commit = _git("commit-tree", tree, "-p", parent, "-m", f"nightly: build {build}")
    _git("push", "origin", f"{commit}:refs/heads/{branch}")
    print(f"  published manifest to origin/{branch} (commit {commit[:9]})")


def gh_upload(zip_path, repo, tag):
    """Upload the zip as an asset on the nightly pre-release (creating it once)."""
    # Ensure the pre-release exists (id is the reused tag); ignore "already exists".
    subprocess.run(
        ["gh", "release", "create", tag, "--repo", repo, "--prerelease",
         "--title", "Nightly builds",
         "--notes", "Rolling nightly channel. Do not use as a stable release."],
        capture_output=True, text=True)
    res = subprocess.run(
        ["gh", "release", "upload", tag, zip_path, "--repo", repo, "--clobber"],
        capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"gh upload failed:\n{res.stderr}")
    print(f"  uploaded asset to {repo} release '{tag}'")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Publish a DJ-CrateBuilder nightly build")
    ap.add_argument("--dist", required=True,
                    help="PyInstaller onedir folder, e.g. dist/DJ-CrateBuilder")
    ap.add_argument("--build", type=int, required=True,
                    help="new build number (must match APP_BUILD in the source)")
    ap.add_argument("--notes", default="", help="short user-facing changelog")
    ap.add_argument("--repo", default=REPO, help="owner/repo for the release")
    ap.add_argument("--out", default="update.json",
                    help="where to write the manifest (default: ./update.json)")
    ap.add_argument("--no-upload", action="store_true",
                    help="skip the gh upload step (zip + manifest only)")
    ap.add_argument("--publish", action="store_true",
                    help="also commit + push update.json to the nightly branch "
                         "(via git plumbing — never touches your working tree)")
    args = ap.parse_args(argv)

    full = f"{APP_VERSION}.{args.build}"
    zip_name = f"DJ-CrateBuilder-{full}.zip"
    print(f"Building nightly {full}")

    zip_dist(args.dist, zip_name)
    digest = _sha256(zip_name)
    print(f"  sha256 {digest}")

    if not args.no_upload:
        gh_upload(zip_name, args.repo, NIGHTLY_TAG)

    url = (f"https://github.com/{args.repo}/releases/download/"
           f"{NIGHTLY_TAG}/{zip_name}")
    manifest = {
        "version": APP_VERSION,
        "build": args.build,
        "url": url,
        "sha256": digest,
        "notes": args.notes,
    }
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(manifest_text)
    print(f"  wrote {args.out}")

    if args.publish:
        publish_manifest(manifest_text, args.build)
        print("\nDone. The new build is live on the nightly channel.")
    else:
        print("\nNext: commit this update.json to the `nightly` branch (or re-run "
              "with --publish to do it automatically). Remember APP_BUILD in the "
              f"source must equal {args.build} in the build you uploaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
