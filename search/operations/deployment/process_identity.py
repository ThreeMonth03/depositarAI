"""Pure process-start fingerprint shared by authenticated runtime proofs.

Callers own validation of the inspect rows, health, resources and authority.
This digest neither authorizes a runtime nor permits restarting its processes.
"""

import hashlib

from search.operations.shared import private_io


def start_identity_sha256(by_role):
    """Hash role-to-inspect rows after the caller has validated them."""
    return hashlib.sha256(
        private_io.canonical(
            {
                role: {
                    "pid": row["State"]["Pid"],
                    "started_at": row["State"]["StartedAt"],
                }
                for role, row in sorted(by_role.items())
            }
        )
    ).hexdigest()
