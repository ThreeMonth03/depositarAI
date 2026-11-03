"""Select the queue's fixed data, socket and client namespace boundaries."""

from pathlib import Path

from search.operations.shared import namespace


def check_transport(registry):
    """Check the fixed data, socket and client directories before dispatch."""
    for directory in (
        Path(registry["root"]),
        Path(registry["runtime_root"]),
        Path(registry["pueue"]).parent,
    ):
        namespace.check_directory(directory, private=True)
