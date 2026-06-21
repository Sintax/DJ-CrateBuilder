"""One-time setup of the `nightly` update channel branch.

Creates an orphan `nightly` branch that holds a SINGLE file, update.json, using
git plumbing (hash-object / mktree / commit-tree). This never switches your
current branch and never touches your working tree — it builds the commit
object directly and just points a new branch ref at it. That keeps `main` and
your working checkout completely undisturbed.

Run once, from the repo root:

    python scripts/init_nightly.py            # create local branch, then asks to push
    python scripts/init_nightly.py --push     # create and push without asking
    python scripts/init_nightly.py --no-push  # create local branch only

After this exists, publish builds with:  python scripts/release_nightly.py ...
"""
import argparse
import os
import subprocess
import sys

DEFAULT_MANIFEST = """\
{
  "version": "1.3",
  "build": 1,
  "url": "",
  "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "notes": "Initial nightly manifest. release_nightly.py overwrites this."
}
"""


def git(*args, stdin=None, check=True):
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create the nightly update branch")
    ap.add_argument("--push", action="store_true", help="push without prompting")
    ap.add_argument("--no-push", action="store_true", help="don't push at all")
    args = ap.parse_args(argv)

    # Must be inside a git repo.
    git("rev-parse", "--git-dir")

    # Refuse if nightly already exists locally or on origin.
    if git("branch", "--list", "nightly"):
        sys.exit("A local `nightly` branch already exists — nothing to do.")
    remote = subprocess.run(["git", "ls-remote", "--heads", "origin", "nightly"],
                            capture_output=True, text=True).stdout.strip()
    if remote:
        sys.exit("origin/nightly already exists — fetch it instead of re-creating.")

    # Build the commit entirely from plumbing — no checkout, no working-tree change.
    blob = git("hash-object", "-w", "--stdin", stdin=DEFAULT_MANIFEST)
    tree = git("mktree", stdin=f"100644 blob {blob}\tupdate.json\n")
    commit = git("commit-tree", tree, "-m", "nightly channel: initial manifest")
    git("branch", "nightly", commit)
    print(f"Created local orphan branch `nightly` (commit {commit[:9]}) with update.json.")

    if args.no_push:
        print("Skipped push. When ready:  git push -u origin nightly")
        return 0

    do_push = args.push
    if not do_push:
        ans = input("Push `nightly` to origin now? [y/N]: ").strip().lower()
        do_push = ans in ("y", "yes")
    if do_push:
        # Full refspec on both sides — `gh release create nightly` creates a
        # git tag of the same name, and `git push origin nightly` then errors
        # with "src refspec nightly matches more than one".
        git("push", "origin", "refs/heads/nightly:refs/heads/nightly")
        git("branch", "--set-upstream-to=origin/nightly", "nightly")
        print("Pushed origin/nightly. The update channel is live.")
    else:
        print("Not pushed. When ready:  "
              "git push origin refs/heads/nightly:refs/heads/nightly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
