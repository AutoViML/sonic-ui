# Contributing

Thanks for contributing to Sonic.

## Development Setup

1. Create and activate a virtual environment.
2. Install dependencies from `requirements.txt`.
3. Run tests before opening a PR.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Pull Requests

- Keep PRs focused and small when possible.
- Include tests for behavior changes.
- Update `README.md` when user-facing behavior changes.
- Do not commit local runtime artifacts (`*.db`, caches, virtualenvs).
- Do not commit local planning docs such as `PRD.md` / `PRD2.md`.

## Style

- Python 3.11+ compatible code.
- Keep code async-safe and avoid event-loop blocking operations.
