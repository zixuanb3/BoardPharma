"""Purpose: Isolate quarterly SSR runs by roster-membership policies.
Process: Validate switches and insert enabled membership folders below ssrquarter.
Input: Existing SSR workflow paths and the ever_board and ieoc_ref switches.
Output: Membership-specific paths with legacy-zero compatibility.
"""

from pathlib import Path

# ========================== USER CONFIG ==========================
# 0: original leadership-only coverage; 1: all-role employment coverage.
EVER_BOARD_DEFAULT = 0
# 0: unconditional BP annual expansion; 1: IE/OC-covered quarters within observed years.
IEOC_REF_DEFAULT = 0
# ================================================================


def validate_policies(
    ever_board: int,
    ieoc_ref: int = 0,
) -> None:
    """Reject unknown switches and the unsupported IE/OC-without-ever-board mode."""
    if ever_board not in (0, 1):
        raise ValueError("ever_board must be 0 or 1")
    if ieoc_ref not in (0, 1):
        raise ValueError("ieoc_ref must be 0 or 1")
    if ieoc_ref == 1 and ever_board != 1:
        raise ValueError("ieoc_ref=1 requires ever_board=1")


def _policy_path(path: Path, folders: tuple[str, ...]) -> Path:
    """Insert missing policy folders in fixed order without duplicating them."""
    if not folders:
        return path
    parts = list(path.parts)
    if "ssrquarter" in parts:
        index = parts.index("ssrquarter") + 1
        for folder in folders:
            if folder in parts:
                index = parts.index(folder) + 1
            else:
                parts.insert(index, folder)
                index += 1
        return Path(*parts)
    result = path
    for folder in folders:
        if folder not in result.parts:
            result /= folder
    return result


def membership_path(path: Path, ever_board: int, ieoc_ref: int = 0) -> Path:
    """Return the roster/event path determined by membership policies only."""
    validate_policies(ever_board, ieoc_ref)
    folders = tuple(
        folder
        for enabled, folder in (
            (ever_board, "ever_board_1"),
            (ieoc_ref, "ieoc1"),
        )
        if enabled
    )
    return _policy_path(path, folders)


def policy_path(path: Path, ever_board: int) -> Path:
    """Preserve the original two-argument helper for existing callers."""
    return membership_path(path, ever_board)
