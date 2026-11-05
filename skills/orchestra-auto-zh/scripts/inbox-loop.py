#!/usr/bin/env python3
"""Retired unsafe inbox consumer; leave historical state untouched."""
import sys


def main():
    print("Retired: use Orca native wakeups and its version-matched orchestration guide; "
          "this consumer is unsafe with auto-handled check deliveries. Existing state is untouched.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
