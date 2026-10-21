"""Render the small trusted-principal adapter; OIDC and crypto stay upstream."""

import hashlib
import json
from pathlib import Path


def identity_lua():
    return Path(__file__).with_suffix(".lua").read_text(encoding="utf-8")


def render_identity(config, *, redirect=False):
    options = {
        "access_mode": config.access_mode,
        "issuer": config.oidc_issuer,
        "client_id": config.oidc_client_id,
        "bucket": hashlib.sha256(config.public_origin.encode("ascii")).hexdigest(),
        "quota_per_guest": config.quota_per_guest,
        "quota_per_account": config.quota_per_account,
        "quota_guest_pool": config.quota_guest_pool,
        "quota_user_pool": config.quota_user_pool,
        "quota_window_seconds": config.quota_window_seconds,
        "session_window_seconds": config.session_window_seconds,
        "redirect": redirect,
    }
    encoded = json.dumps(json.dumps(options, sort_keys=True, separators=(",", ":")))
    return (
        "return require('depositar_identity').handler("
        "require('cjson.safe').decode(" + encoded + "))"
    )
