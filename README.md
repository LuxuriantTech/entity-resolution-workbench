# Entity Resolution Workbench

Compare two synthetic supplier catalogues, inspect field similarities and keep ambiguous pairs in a review queue.

## Try the synthetic example

Requires Python 3.12.14 and uv. The initial installation downloads locked development dependencies; the installed runtime uses the Python standard library.

From this checkout:

```sh
uv sync --frozen
.venv/bin/python scripts/workbench_control.py start --port 8765
```

Open the loopback URL printed by the launcher. Prepare the bundled synthetic catalogues, run the matcher and open Needs review. Each pair exposes both records, component similarities and decision reasons. MATCH, REVIEW and NO_MATCH are valid outcomes; REVIEW preserves uncertainty. Similarities are not probabilities.

Stop the server when finished:

```sh
.venv/bin/python scripts/workbench_control.py stop
```

## What this public release contains

This is a clean public source snapshot of the local project, not a copy of its private Git history. Application source files and synthetic input files are unchanged from the verified local implementation. Private orchestration records, author paths, archived build-proof machinery and one-shot research runners are not distributed. Their canonical local versions and history remain preserved.

The tests shipped here cover the public runtime. They are a defined subset of the larger local verification suite, not a claim that every historical control is reproduced by this package. `SOURCE_MANIFEST.json` identifies every copied file, and `PUBLIC_RELEASE_SCOPE.json` lists the selected runtime tests and the omitted verification categories.

```sh
uv run --frozen pytest -q
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy src
# Before a commit, with gitleaks installed:
gitleaks protect --staged --redact --verbose
```

## Limits

The sample is synthetic and the thresholds are project-specific. A session annotation does not change the deterministic matching engine or establish business accuracy.

The browser server is designed for a local machine. Do not expose it directly on the Internet. The recruiter preview is a separate static explanation with recorded synthetic results.

## My role

I use AI extensively to build these projects. I understand and review the code, and I am still learning to write it independently.
