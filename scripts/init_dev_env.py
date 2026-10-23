"""Create an isolated local Docker profile without overwriting existing secrets."""

import argparse
import os
import secrets
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BACKEND_ROOT / ".env.development")
    args = parser.parse_args()
    template = (BACKEND_ROOT / ".env.development.example").read_text()
    content = template.replace("GENERATE_WITH_SETUP", secrets.token_urlsafe(32))
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"Kept existing profile: {args.output}")
        return
    with os.fdopen(fd, "w") as output:
        output.write(content)
    print(f"Created development profile: {args.output}")


if __name__ == "__main__":
    main()
