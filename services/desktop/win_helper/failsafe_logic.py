from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    CORNER_FAILSAFE_CODE,
    CORNER_FAILSAFE_PX,
    CORNER_MIN_PX,
    MEIVA_INPUT_TAG,
)


def is_in_corner(x: int, y: int, threshold: int = CORNER_FAILSAFE_PX) -> bool:
    return CORNER_MIN_PX <= x <= threshold and CORNER_MIN_PX <= y <= threshold


def is_bridge_input(extra_info: int) -> bool:
    return (int(extra_info) & 0xFFFFFFFF) == MEIVA_INPUT_TAG


@dataclass(frozen=True)
class CornerFailsafeState:
    threshold_px: int = CORNER_FAILSAFE_PX
    aborted: bool = False
    aborted_at: str | None = None
    aborted_position: tuple[int, int] | None = None
    aborted_previous_holder: str | None = None
    abort_count: int = 0

    def should_trigger_from_hook(
        self, x: int, y: int, *, extra_info: int, has_active_session: bool
    ) -> bool:
        if is_bridge_input(extra_info) or not has_active_session:
            return False
        return is_in_corner(x, y, self.threshold_px)

    def with_abort(
        self, pos: tuple[int, int], *, now_iso: str, previous_holder: str | None = None
    ) -> tuple[CornerFailsafeState, bool]:
        transitioned = not self.aborted
        next_state = CornerFailsafeState(
            threshold_px=self.threshold_px,
            aborted=True,
            aborted_at=now_iso,
            aborted_position=(int(pos[0]), int(pos[1])),
            aborted_previous_holder=(
                previous_holder if previous_holder is not None else self.aborted_previous_holder
            ),
            abort_count=self.abort_count + (1 if transitioned else 0),
        )
        return next_state, transitioned

    def abort_error(self, *, pos: tuple[int, int] | None = None) -> dict[str, object]:
        at_pos = pos if pos is not None else self.aborted_position
        pos_list = [at_pos[0], at_pos[1]] if at_pos is not None else None
        return {
            "ok": False,
            "code": CORNER_FAILSAFE_CODE,
            "position": pos_list,
            "aborted_at": self.aborted_at,
            "previous_holder": self.aborted_previous_holder,
            "message": (
                f"Session aborted by top-left corner failsafe (cursor entered "
                f"0..{self.threshold_px} hot zone at {pos_list}). Active lease was released "
                "and orange border overlay hidden. Wait for the user to move the mouse away "
                "from the top-left corner, then acquire a fresh session to resume."
            ),
        }

    def cleared_for_new_session(
        self, cursor_pos: tuple[int, int] | None
    ) -> tuple[CornerFailsafeState, dict[str, object] | None]:
        if cursor_pos is not None and is_in_corner(cursor_pos[0], cursor_pos[1], self.threshold_px):
            return self, self.abort_error(pos=(int(cursor_pos[0]), int(cursor_pos[1])))
        cleared = CornerFailsafeState(
            threshold_px=self.threshold_px,
            aborted=False,
            aborted_at=None,
            aborted_position=None,
            aborted_previous_holder=None,
            abort_count=self.abort_count,
        )
        return cleared, None
