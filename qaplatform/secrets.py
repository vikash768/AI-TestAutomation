"""Symmetric encryption for run credentials at rest.

A run request carries the test account's password. It must reach the worker
process through the database, so it is stored encrypted with a key derived
from QA_SECRET_KEY and wiped when the run finishes.
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secrets(secret_key: str, payload: dict) -> str:
    return _fernet(secret_key).encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")


def decrypt_secrets(secret_key: str, token: str | None) -> dict:
    if not token:
        return {}
    return json.loads(_fernet(secret_key).decrypt(token.encode("ascii")).decode("utf-8"))
