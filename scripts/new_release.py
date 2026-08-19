#!/usr/bin/env python3
"""Scaffolds a new release notes version so a human never has to remember
which of the 5 files to touch by hand.

It performs exactly the mechanical wiring that scripts/check_release_notes.py
validates - nothing more:

  1. copies the previous version's page as a starting template for the new one
  2. adds its nav entry (to mkdocs.yml if that branch overrides nav there,
     otherwise to mkdocs-base.yml, same as mkdocs itself resolves it)
  3. adds its bullet to the release notes index page
  4. adds a `date.<key>: TBD` placeholder to variables.yml
  5. bumps the version number on the PDF cover page template

Every edit is either an append-only insertion (new line next to a known
anchor) or a scoped string substitution - it never rewrites a file
wholesale, so it can't do what the old sed-based date automation did
(silently clobbering unrelated keys). It also never touches component
versions, the CVE list, or tarball versions - those are actual release
content that needs a human, not a template.

Nothing is committed. Review the result with `git diff` and edit the new
page's content before committing.

Usage:
    scripts/new_release.py 18.7.1                    # base = latest in nav
    scripts/new_release.py 18.7.1 --based-on 18.6.1  # explicit base
    scripts/new_release.py 18.7.1 --year 2027         # force a new index year section
    scripts/new_release.py 18.7.1 --date 2026-09-15   # release date already known
"""
import argparse
import datetime
import re
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_release_notes as crn  # reuse its yaml loader + nav/index logic

ROOT = Path(__file__).resolve().parent.parent


def version_key(version):
    return version.replace(".", "_")


def detect_layout():
    """14/16/17/18 nest release notes under docs/release-notes/; 15 keeps
    them flat under docs/. Detect whichever this branch actually uses."""
    if (ROOT / "docs" / "release-notes" / "release-notes.md").exists():
        return ROOT / "docs" / "release-notes"
    return ROOT / "docs"


def nav_owner_file():
    """mkdocs INHERIT semantics: mkdocs.yml's own top-level nav (if it
    defines one) fully replaces mkdocs-base.yml's - so that's the file to
    edit. If mkdocs.yml doesn't define nav at all, edit the base file it
    falls back to instead. Same resolution scripts/check_release_notes.py
    uses to read it."""
    child = crn.load_yaml(ROOT / "mkdocs.yml")
    if "nav" in child:
        return ROOT / "mkdocs.yml"
    return ROOT / (child.get("INHERIT") or "mkdocs-base.yml")


def ordered_nav_paths(nav):
    """Like crn.collect_nav_paths, but preserves nav order - needed here
    because entries are listed newest-first by convention and we rely on
    that order to find "the latest version"."""
    acc = []
    for item in nav:
        if isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str):
                    acc.append(value)
                else:
                    acc.extend(ordered_nav_paths(value))
        elif isinstance(item, str):
            acc.append(item)
    return acc


def latest_version_from_nav():
    for path in ordered_nav_paths(crn.effective_nav()):
        m = re.match(r"^(?:release-notes/)?release-notes-v([\d.]+)\.md$", path)
        if m:
            return m.group(1)
    return None


def insert_line_after(path, anchor_pattern, make_line, description):
    """Insert make_line(indent) right after the first line matching
    anchor_pattern. Never touches any other line. Fails loudly instead of
    guessing if the anchor isn't found."""
    lines = path.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if anchor_pattern.search(line):
            indent = re.match(r"[ \t]*", line).group(0)
            new_line = make_line(indent)
            if not new_line.endswith("\n"):
                new_line += "\n"
            lines.insert(i + 1, new_line)
            path.write_text("".join(lines))
            return
    raise SystemExit(f"Could not find anchor for {description} in {path.relative_to(ROOT)} - inserted nothing.")


def scaffold_page(base_file, dest_file, base_version, new_version):
    text = base_file.read_text()
    if base_version not in text:
        print(f"note: {base_version!r} doesn't appear verbatim in {base_file.name} - "
              f"version substitution in the new page may be incomplete, check it.")
    text = text.replace(base_version, new_version)
    text = text.replace(f"date.{version_key(base_version)}", f"date.{version_key(new_version)}")
    dest_file.write_text(text)


def update_variables_yml(new_version, release_date):
    path = ROOT / "variables.yml"
    key = version_key(new_version)
    value = release_date.isoformat() if release_date else "TBD"
    if re.search(rf"^\s*{re.escape(key)}\s*:", path.read_text(), re.MULTILINE):
        print(f"variables.yml already has a date.{key} entry, leaving it alone.")
        return
    insert_line_after(
        path,
        re.compile(r"^date:\s*$"),
        lambda indent: f"  {key}: {value}\n",
        f"date.{key} entry",
    )


def update_pdf_cover(new_version, release_date):
    path = ROOT / "docs" / "templates" / "pdf_cover_page.tpl"
    if not path.exists():
        print("note: docs/templates/pdf_cover_page.tpl not found, skipping.")
        return
    # matches the existing convention, e.g. "<h2>17.10.2 (July 06, 2026)</h2>"
    date_str = release_date.strftime("%B %d, %Y") if release_date else "TBD"
    text = path.read_text()
    new_text, n = re.subn(r"<h2>[\d.]+ \([^)]*\)</h2>", f"<h2>{new_version} ({date_str})</h2>", text)
    if n == 0:
        raise SystemExit("Could not find the <h2>VERSION (date)</h2> line in pdf_cover_page.tpl.")
    path.write_text(new_text)


def update_nav(new_version, filename_rel):
    path = nav_owner_file()
    key = version_key(new_version)
    line = f'- "{new_version} ({{{{date.{key}}}}})": {filename_rel}'
    insert_line_after(
        path,
        re.compile(r'"Release notes index"\s*:'),
        lambda indent: f"{indent}{line}\n",
        f"nav entry for {new_version}",
    )


def update_index(new_version, filename_basename, year):
    docs_dir = detect_layout()
    index_path = docs_dir / "release-notes.md"
    key = version_key(new_version)
    bullet = f"* [{new_version}]({filename_basename}) ({{{{date.{key}}}}})"
    if year:
        text = index_path.read_text()
        if re.search(rf"^## {year}\s*$", text, re.MULTILINE):
            insert_line_after(index_path, re.compile(rf"^## {year}\s*$"), lambda indent: bullet, f"{new_version} bullet")
        else:
            # new year section: insert right before the first existing '## '
            lines = text.splitlines(keepends=True)
            for i, line in enumerate(lines):
                if line.startswith("## "):
                    lines[i:i] = [f"## {year}\n", "\n", bullet + "\n", "\n"]
                    index_path.write_text("".join(lines))
                    break
            else:
                raise SystemExit("Could not find any '## <year>' section to anchor a new one before.")
    else:
        insert_line_after(index_path, re.compile(r"^## "), lambda indent: bullet, f"{new_version} bullet")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("new_version", help="e.g. 18.7.1")
    parser.add_argument("--based-on", dest="base_version", help="version to copy as a template (default: latest in nav)")
    parser.add_argument("--year", help="force a new '## <year>' section in the index page (default: top existing section)")
    parser.add_argument("--date", help="release date if already known, YYYY-MM-DD - sets it in variables.yml and the PDF cover page instead of TBD")
    args = parser.parse_args()

    release_date = None
    if args.date:
        try:
            release_date = datetime.date.fromisoformat(args.date)
        except ValueError:
            raise SystemExit(f"--date must be YYYY-MM-DD, got {args.date!r}")

    base_version = args.base_version or latest_version_from_nav()
    if not base_version:
        raise SystemExit("Could not auto-detect the latest version - pass --based-on explicitly.")
    if base_version == args.new_version:
        raise SystemExit("--based-on can't be the same as the new version.")

    docs_dir = detect_layout()
    base_file = docs_dir / f"release-notes-v{base_version}.md"
    if not base_file.exists():
        raise SystemExit(f"Base file not found: {base_file.relative_to(ROOT)}")
    new_file = docs_dir / f"release-notes-v{args.new_version}.md"
    if new_file.exists():
        raise SystemExit(f"{new_file.relative_to(ROOT)} already exists.")

    filename_rel = str(PurePosixPath(*new_file.relative_to(ROOT / "docs").parts))
    filename_basename = new_file.name

    print(f"Scaffolding {args.new_version} from {base_version} ({docs_dir.relative_to(ROOT)}/ layout)...")
    scaffold_page(base_file, new_file, base_version, args.new_version)
    print(f"  wrote {new_file.relative_to(ROOT)}")
    update_nav(args.new_version, filename_rel)
    print(f"  added nav entry to {nav_owner_file().relative_to(ROOT)}")
    update_index(args.new_version, filename_basename, args.year)
    print(f"  added index bullet to {(docs_dir / 'release-notes.md').relative_to(ROOT)}")
    update_variables_yml(args.new_version, release_date)
    print(f"  added variables.yml date entry ({release_date.isoformat() if release_date else 'TBD'})")
    update_pdf_cover(args.new_version, release_date)
    print("  updated docs/templates/pdf_cover_page.tpl")

    print("\nStill needs a human:")
    print(f"  - component version table, tarball versions, CVE list, upstream PostgreSQL link in {new_file.relative_to(ROOT)}")
    print("  - variables.yml: pspgversion/release/dockertag and any bumped component versions (pg_tde, pgBackRest, ...)")
    if not release_date:
        print("  - the real release date, once known, in variables.yml and pdf_cover_page.tpl (currently TBD)")

    print("\nValidating wiring...")
    import subprocess
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "check_release_notes.py")])
    if result.returncode != 0:
        print("\ncheck_release_notes.py reported errors above - fix before committing.", file=sys.stderr)
        sys.exit(result.returncode)

    print("\nReview with `git diff`, fill in the content, then commit.")


if __name__ == "__main__":
    main()
