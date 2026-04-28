"""``python -m alpha_harness.audit`` — run every auditor, exit non-zero on failure."""

from __future__ import annotations

import sys

from alpha_harness.audit import AuditError, run_all_audits


def main() -> int:
    try:
        run_all_audits()
    except AuditError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("audit: all clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
