# Entity Resolution Workbench

[Try the interactive synthetic demo](https://ardian-mehaj-portfolio.vercel.app/projects/entity-resolution-workbench/) · [Demo source and local preview](docs/interactive-demo/README.md) · [Contact me](mailto:mehajardian@gmail.com)

Compare two synthetic supplier catalogues, inspect field similarities and keep ambiguous pairs in a review queue.

## Try the synthetic example

Requires Python 3.12.14 and uv. The initial installation downloads locked development dependencies; the installed runtime uses the Python standard library.

From this checkout:

```sh
uv sync --frozen
.venv/bin/python scripts/workbench_control.py start --port 8765
```

Open the loopback URL printed by the launcher. Prepare the bundled synthetic catalogues, run the matcher and open Needs review. Each pair exposes both records, component similarities and decision reasons. MATCH, REVIEW and NO_MATCH are valid outcomes; REVIEW preserves uncertainty. Similarities are not probabilities.

The hosted [static walkthrough](docs/interactive-demo/README.md) has a threshold control that changes only its explanatory annotation. It does not rerun the matcher or alter an engine decision. In the local workbench, `REVIEW` is the deterministic engine outcome for an ambiguous pair; a separate session-local human annotation can record a review choice without changing that outcome.

Stop the server when finished:

```sh
.venv/bin/python scripts/workbench_control.py stop
```

## What this public release contains

This is a clean public source snapshot of the local project, not a copy of its private Git history. Application source files and synthetic input files are unchanged from the verified local implementation. Private orchestration records, author paths, archived build-proof machinery and one-shot research runners are not distributed. Their canonical local versions and history remain preserved.

The tests shipped here cover the public runtime. They are a defined subset of the larger local verification suite, not a claim that every historical control is reproduced by this package. `SOURCE_MANIFEST.json` identifies every copied file, and `PUBLIC_RELEASE_SCOPE.json` lists the selected runtime tests and the omitted verification categories.

The full `pytest` command also needs local Google Chrome available as `google-chrome` or `google-chrome-stable` for the narrow-render tests. The matcher and local workbench demo do not need Chrome.

```sh
uv run --frozen pytest -q
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy src
# Before a commit, with gitleaks installed:
gitleaks protect --staged --redact --verbose
```

The prepared [CI workflow](.github/workflows/ci.yml) runs the public tests, Ruff and mypy on GitHub after publication. Its presence here is not a hosted CI result.

Local check on 2026-09-23 (this public snapshot): `uv run --frozen pytest -q` returned 199 passed; the Ruff format and lint commands above passed, and `uv run --frozen mypy src` found no issues in 18 source files. This is software verification of the distributed test selection, not a measured matching precision or business accuracy result.

## Limits

The sample is synthetic and the thresholds are project-specific. A session annotation does not change the deterministic matching engine or establish business accuracy.

The browser server is designed for a local machine. Do not expose it directly on the Internet. The recruiter preview is a separate static explanation with recorded synthetic results.

## My role

I use AI extensively to build these projects. I understand and review the code, and I am still learning to write it independently.
