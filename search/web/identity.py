"""Accept identity only from the operator's authenticated entrance."""

import re
import secrets

from search.secrets import read_secret
from .sessions import SessionError


class EdgeIdentity:
    def __init__(self, secret_file=None):
        # Read once at startup, before serving or allocating browser sessions.
        self.secret = None if secret_file is None else read_secret(secret_file)

    @property
    def required(self):
        return self.secret is not None

    def authenticate(self, headers):
        if not self.required:
            return None
        subjects = []
        keys = []
        for name, value in headers:
            if name.lower() == b"x-depositar-subject":
                subjects.append(value)
            elif name.lower() == b"x-depositar-edge-key":
                keys.append(value)
        if (
            len(subjects) != 1
            or len(keys) != 1
            or re.fullmatch(rb"[0-9a-f]{64}", subjects[0]) is None
            or not secrets.compare_digest(keys[0], self.secret.encode("ascii"))
        ):
            raise SessionError(401, "authentication_required")
        return subjects[0].decode("ascii")
