# Home shell layout chain

`ui/web/src/lib/layout/layout.ts` splits the flex primitives: `FLEX` is
`flex`, `FLEX_COL` is `flex-col` only. A column wrapper around the shell
therefore needs **both**; `FLEX_COL` alone leaves `display: block`, and a
block's height is its content height, so nothing below can shrink.

`HomeLayout`'s narrow branch returns a fragment (`sidebar`, `main`, `inspector`)
whose children are flex items of *their parent* -- so the wrapper the page puts
around the shell must stay a row.
