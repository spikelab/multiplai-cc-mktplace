#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "google-auth-oauthlib",
# ]
# ///
"""get_token.py — one-time Gmail OAuth consent. RUN ON THE MAC HOST, not the container.

Why the host: the InstalledApp flow opens a browser and listens on a localhost
redirect. Both live on the Mac, not inside the Docker container.

What it does:
  * Runs the OAuth InstalledApp flow against your Desktop OAuth client secret.
  * Requests exactly two scopes: gmail.compose + gmail.readonly (nothing else).
  * PRINTS the long-lived credential as three env vars for you to paste into the
    kit's .env — mirroring how SLACK_TOKEN works. Google's credential is a trio
    (client id + secret + refresh token), not one bearer string, because access
    tokens expire hourly and are minted on the fly from the refresh token.

Usage (recommended, via uv — resolves deps automatically):
  uv run get_token.py /path/to/client_secret.json
  → prints GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN

Optional: also write a JSON token file (for the GMAIL_TOKEN_FILE fallback path):
  uv run get_token.py client_secret.json --out /abs/path/token.json
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint the Gmail read+draft credential.")
    ap.add_argument("client_secret", help="path to the Desktop OAuth client "
                    "secret JSON downloaded from Google Cloud Console")
    ap.add_argument("--out", default=None,
                    help="also write the credential as a JSON token file at this "
                    "path (for the GMAIL_TOKEN_FILE fallback); default: env-only")
    args = ap.parse_args()

    if not os.path.isfile(args.client_secret):
        print(f"error: client secret not found: {args.client_secret}",
              file=sys.stderr)
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("error: google-auth-oauthlib is not installed.\n"
              "Run this script with uv (`uv run get_token.py ...`) or install it:\n"
              "  pip install google-auth-oauthlib", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, SCOPES)
    # Opens the browser, listens on a free localhost port for the redirect.
    creds = flow.run_local_server(port=0, prompt="consent")

    granted = set(creds.scopes or [])
    extra = granted - set(SCOPES)
    if extra:
        print("warning: Google granted extra scopes beyond compose+readonly: "
              + ", ".join(sorted(extra)) + "\nThe skill will REFUSE to run with "
              "these. Re-consent and grant only the two requested scopes.",
              file=sys.stderr)

    client_id = flow.client_config["client_id"]
    client_secret = flow.client_config["client_secret"]
    refresh_token = creds.refresh_token
    if not refresh_token:
        print("error: no refresh_token returned. Revoke the app's access at "
              "https://myaccount.google.com/permissions and re-run.",
              file=sys.stderr)
        return 1

    # Primary output: env vars for .env (mirrors SLACK_TOKEN).
    print("\n# --- Add these to your kit .env (never commit) ---")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={refresh_token}")
    print("# scopes granted:", ", ".join(sorted(granted)))
    print("# Then restart the container and ask to draft a reply to confirm.\n")

    # Optional: also drop a JSON token file for the file-fallback path.
    if args.out:
        out = Path(args.out)
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "token_uri": creds.token_uri,
            "scopes": sorted(granted),
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        print(f"Also wrote token file {out} (mode 0600).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
