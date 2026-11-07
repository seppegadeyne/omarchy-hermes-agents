# Constraints

Last reviewed: 2026-11-07

## Floor (always enforced)
- No new suppression comments (`# noqa`, `# type: ignore`) without a reason; no bare `except:`.
- No stubs or silent failures (`raise NotImplementedError`, `except Exception: pass`).
- No skipped or deleted tests without a reason in the commit message.
- No secrets or tokens in source or logs.
- This file is not weakened to make a change pass.

## Enforced with numbers
| Dimension | Rule | Checked by | Runs at |
|---|---|---|---|
| Overview tests | all pass | `python3 tests/test_overview.py` | every change |
| Resilience tests | all pass | `python3 tests/test_resilience.py` | every change |

No lint, type check or CI exists in this repo.

## Measured, not yet enforced (ratchets)
| Metric | Today | Direction | Tolerance |
|---|---|---|---|
| Suppression comments | 1 | must not grow | 0 |
| Bare `except:` | 0 | must not grow | 0 |

Commands: `git grep -nE "# noqa|# type: ignore" -- '*.py' | wc -l` and `git grep -nE "except:\s*$" -- '*.py' | wc -l`.

## Exceptions
| ID | Rule | Path | Reason | Expires |
|---|---|---|---|---|
