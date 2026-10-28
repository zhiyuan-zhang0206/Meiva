export const BAR_HEIGHT_PX = 44;
export const BAR_HEIGHT_CLASS = "h-11";
export const BAR_DIVIDER_CLASS =
  "after:pointer-events-none after:absolute after:inset-x-1 after:bottom-0 after:h-px after:bg-border";
export const BAR_MIN_HEIGHT_CLASS = "min-h-11";

// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
export const COLUMN_TITLE_BAR_CLASS = `relative ${BAR_HEIGHT_CLASS} ${BAR_DIVIDER_CLASS}`;
export const COLUMN_TITLE_BAR_WRAP_CLASS = `relative ${BAR_MIN_HEIGHT_CLASS} ${BAR_DIVIDER_CLASS}`;

// meiva:note
// The strip every home column ends with, and the one rule the columns share
// across the bottom of the screen (user ruling 2026-10-29). Both columns are
// bottom-anchored, so equal rules mean equal bands: a band that composes its own
// 1px + padding + control row lands its rule wherever those parts add up to, and
// two such bands drift apart by exactly the difference — the sidebar's sat 16px
// below the composer's before this primitive existed. BOTTOM_BAND_ROW_CLASS is
// the band's control row: the composer's is 36px (its `size-9` upload button and
// `min-h-9` textarea) and the sidebar footer's icon row is 28px, so the minimum
// is what makes them the same row. BOTTOM_BAND_HEIGHT_PX is derived, never
// tuned: rule (1) + pt-2 (8) + row (36) + pb-3 (12).
// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
export const BOTTOM_BAND_CLASS = "border-t border-border pt-2 pb-3";
export const BOTTOM_BAND_ROW_CLASS = "min-h-9";
export const BOTTOM_BAND_HEIGHT_PX = 1 + 8 + 36 + 12;
