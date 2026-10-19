"""Mint credential surrogates for the East Equity pipeline.

Reads the Secure Vault-backed custom connectors and prints `export` lines the
pipeline's env-var auth understands. Surrogates are NOT secrets: they are
references the runtime swaps for the real credential on egress to the
connector's registered hosts. Mint fresh every run; never store the output.

Connectors (all created via the secure vault, one-time):
  custom.alpaca-key-id     -> ALPACA_API_KEY      (paper-api + data hosts only)
  custom.alpaca-key-secret -> ALPACA_SECRET_KEY
  custom.fred              -> FRED_API_KEY
  custom.x-user            -> X_BEARER_TOKEN      (OAuth2 user token, for X posts)

Exit 0 and prints exports when all four resolve. Exit 2 listing the missing
connectors otherwise, so the conductor can stand down quietly.
"""
import sys

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")

from dynamic_credentials import dynamic_credential_entry, DynamicCredentialError

WANT = [
    ("custom.alpaca-key-id", "ALPACA_API_KEY"),
    ("custom.alpaca-key-secret", "ALPACA_SECRET_KEY"),
    ("custom.fred", "FRED_API_KEY"),
    ("custom.x-user", "X_BEARER_TOKEN"),
]

missing = []
for connector, var in WANT:
    try:
        entry = dynamic_credential_entry(connector)
        surr = str(entry.get("surrogate", "")).strip()
        if not surr.startswith("hsurr:"):
            raise DynamicCredentialError("non-surrogate value")
        # Quote for shell safety; surrogates are opaque tokens.
        print(f"export {var}='{surr}'")
    except Exception:
        missing.append(connector)

if missing:
    print(f"MISSING_CONNECTORS={' '.join(missing)}", file=sys.stderr)
    sys.exit(2)
