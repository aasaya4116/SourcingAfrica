"""
Sourcing Africa — Gmail OAuth Setup
Run this ONCE locally to generate your refresh token.

Prerequisites:
  1. Go to console.cloud.google.com
  2. Create a project → Enable the Gmail API
  3. Create OAuth 2.0 credentials (Desktop app)
  4. Download as credentials.json and place next to this file

Usage:
  python ingestor/gmail_auth.py

Output:
  Prints GMAIL_REFRESH_TOKEN — paste this into your Railway environment variables.
"""

import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDS_FILE = Path(__file__).parent / "credentials.json"


def main():
    if not CREDS_FILE.exists():
        print(
            "\n[ERROR] credentials.json not found.\n\n"
            "Steps:\n"
            "  1. Go to console.cloud.google.com\n"
            "  2. Create a project → APIs & Services → Enable Gmail API\n"
            "  3. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID\n"
            "     Application type: Desktop app\n"
            "  4. Download JSON → rename to credentials.json\n"
            "  5. Place it in the ingestor/ folder\n"
            "  6. Re-run this script\n"
        )
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n✓ Authentication successful.\n")
    print("=" * 60)
    print("Add these to your Railway environment variables:")
    print("=" * 60)
    print(f"GMAIL_CLIENT_ID     = {creds.client_id}")
    print(f"GMAIL_CLIENT_SECRET = {creds.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN = {creds.refresh_token}")
    print("=" * 60)
    print("\nAlso add: ANTHROPIC_API_KEY = sk-ant-...")


if __name__ == "__main__":
    main()
