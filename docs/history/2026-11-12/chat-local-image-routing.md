# Route chat images through their file owner

Task #6833 reproduced a broken image in agent 405's ordinary message. Its PNG
existed in agent 7632's macmini workspace, but Markdown emitted the local
absolute pathname as an image URL. The browser requested that pathname from
the frontend and received a 404 HTML page. The document panel already had an
explicit resource resolver; ordinary chat images had no equivalent mapping.

The existing workspace raw reader returned 404 when asked through agent 405
and 200 image/png through agent 7632. The fix therefore keeps an explicit
workspace owner from the reference rather than assuming the conversation owns
every referenced artifact. Relative and cwd paths retain the selected-agent
interpretation used by file links. The gateway still chooses the owning machine
and enforces its existing workspace/cwd boundary.

Copying every artifact into the conversation's workspace would hide the source
of the routing failure and duplicate files. Serving arbitrary frontend-local
paths would bypass placement and access rules. Neither is needed: this change
uses the existing authenticated byte reader and preserves the document panel's
registered-resource resolver. No gateway permission or filesystem root expands.
