// meiva:note
// One glyph size for the home shell's icon-only controls, and the composer's
// upload glyph is the reference (user ruling 2026-10-29). Every icon button in
// the shell's four per-column rows takes it — the header bar, the sidebar's title
// row, the sidebar footer and the composer's control row — because a row that
// mixes glyph sizes reads as two families drawn by different hands: the right
// panel's toggle and the phone's menu button were 20px beside 16px neighbours.
// Not every glyph in the shell is a control: an inline mark inside a text row
// (the pending strip's clock, the stop glyph on the send button) keeps its own
// optical size, because 16px would break the line it sits in.
// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
export const ICON_SIZE_CLASS = "size-4";
export const ICON_SIZE_PX = 16;
