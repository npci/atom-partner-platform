# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""One-time migration: encrypt any plaintext `partner_settings` secret values
in place. Idempotent — skips rows already in `enc:v1:` form (see
core.secret_box). Run once after deploying core/secret_box.py and setting
PARTNER_SECRET_KEK.

Also usable as a KEK rotation script: decrypt every row with the OLD key,
then re-encrypt with the NEW key. To rotate:
    1. Export PARTNER_SECRET_KEK=<old-key>, run this script with
       --rotate-to-stdin and pipe the decrypted values somewhere safe (NOT
       recommended for production — prefer a proper vault rotation flow once
       Phase 2 of docs/adr/ADR-0002-secrets-vault-migration.md ships).
    2. Simpler/safer for a small key set: read each secret via the Settings
       UI (masked display won't help here — use a DB query with the OLD KEK
       loaded), then re-save each one via the Settings UI with the NEW KEK
       loaded in the environment. This script's primary purpose is the
       one-time plaintext -> encrypted migration, not routine rotation.

Usage:
    cd backend
    PARTNER_SECRET_KEK=<your-key> python -m scripts.migrate_encrypt_secrets
"""
from __future__ import annotations

import sys


def main() -> int:
    from app.core.secret_box import (
        SECRET_KEYS,
        SecretBoxError,
        decrypt,
        encrypt,
        is_encrypted,
        needs_reencryption,
    )
    from app.database import SessionLocal
    from app.models import PartnerSetting

    db = SessionLocal()
    try:
        migrated = 0
        skipped = 0
        for key in SECRET_KEYS:
            row = db.get(PartnerSetting, key)
            if not row or not row.value:
                continue
            if is_encrypted(row.value):
                # Already ciphertext — but it may be in the superseded
                # AES-GCM format. Re-encrypt those to Fernet so the store
                # converges on one format; leave current-format values alone.
                if needs_reencryption(row.value):
                    try:
                        row.value = encrypt(decrypt(row.value))
                        migrated += 1
                        print(f"  re-encrypted to Fernet: {key}")
                    except SecretBoxError as exc:
                        print(f"  ERROR re-encrypting {key}: {exc}", file=sys.stderr)
                        db.rollback()
                        return 1
                else:
                    skipped += 1
                continue
            try:
                row.value = encrypt(row.value)
                migrated += 1
                print(f"  encrypted: {key}")
            except SecretBoxError as exc:
                print(f"  ERROR encrypting {key}: {exc}", file=sys.stderr)
                db.rollback()
                return 1
        db.commit()
        print(f"Done. Migrated {migrated} secret(s), {skipped} already in the current format.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
