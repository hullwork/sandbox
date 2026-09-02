"""Shared limits for workspace file operations.

Control Plane serves an offline path while Runtime embeds the workspace file module.
Both paths must apply the same limits so availability does not change behavior.
"""

MAX_FILE_BYTES = 1_000_000
MAX_READ_CHARS = 8_000
MAX_LIST_ENTRIES = 500
MAX_READ_SOURCE_BYTES = 16 * 1024 * 1024
MAX_READ_LINES = 2_000

# Every Workspace exposes the same durable top-level layout. Both the volume
# role and the Runtime process use this contract so recovery does not depend on
# a separate init container.
WORKSPACE_LAYOUT = ("src", "data/uploads", "artifacts", ".sandbox")
