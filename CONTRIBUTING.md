# Contributing to PULSE

Thanks for helping. This page covers how changes reach this repository and what a pull request needs.

## How this repository is published

PULSE is developed in a private repository and published here as a one-way export: each release replaces the tree with a new commit generated from a reviewed revision. Pull requests here are welcome; accepted changes are applied upstream by a maintainer and appear in the next export, credited to you in the pull request and release notes.

## Before you start

- For bugs, open an issue with the steps, the expected and the actual result, and the relevant log lines with secrets removed.
- For features or larger changes, open an issue first so we can agree on the approach.
- Never include tokens, app secrets, chat IDs, `auth.json` contents or private hostnames in issues, logs or tests. Use placeholders such as `<APP_ID>` and reserved values such as `example.com` and `192.0.2.0/24`.

## Development setup

```bash
cd Relay
npm ci
npm run typecheck
npm run build
npm test
```

Python modules need Python 3.11 or newer:

```bash
python -m pytest agent-harness/tests_rewrite
python -m pytest ComputeCost/tests
python -m pytest Stats/exporter/tests
```

## Pull requests

- Keep each pull request to one change, with tests for new behavior.
- Run the Relay checks above and the secret scan before pushing:

  ```bash
  gitleaks dir . --config .gitleaks.toml
  ```

- Update the matching spec under `*/specs/` when you change documented behavior.
- Describe what changed, why, and how you tested it.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).

## Code of Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
