# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-20

### Added

- Initial release: `scan` command pulls authors from `calibredb` (or a
  plain text file), normalizes names (diacritics, initials, `Last,
  First` reordering), and clusters duplicates into `author_review.csv`
  with `high` / `medium` / `low` / `glued` confidence levels.
- `apply` command reads back the reviewed CSV and merges or splits
  authors through `calibredb set_metadata`, preserving and deduping
  co-authors on multi-author books. Dry run by default; `--commit`
  required to write changes.
- Persistent decision log — approvals write to `always_merge.txt`,
  rejections to `never_merge.txt`, so re-scans stop re-surfacing
  settled calls.
- Single-author merges also correct `author_sort` (`Last, First`) to
  keep sorting consistent after a merge.
- Packaged as an installable CLI (`pip install -e .`) with a
  `calibre-author-cleanup` console entry point, MIT license, and
  README covering install, usage, and safety notes (back up
  `metadata.db`, stop the web front-end before `--commit`).

### Changed

- README expanded with a workflow diagram (`assets/workflow.svg`) and
  an example `author_review.csv` (`assets/review-csv-example.svg`) so
  the confidence levels and review flow are visible before install.

[Unreleased]: https://github.com/gmwestrup/calibre-author-cleanup/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gmwestrup/calibre-author-cleanup/releases/tag/v0.1.0
