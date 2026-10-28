highlight Comment ctermfg=Blue ctermbg=Black
highlight Arrow ctermfg=Green
highlight Search ctermfg=Red ctermbg=NONE

python << EOF
from rich.style import Style

# cgdb colour names map to Rich colours (Blue -> bright_blue, Black -> black).
comment = app.hl.get("Comment")
assert comment.fg == "bright_blue", f"Comment fg: expected bright_blue, got {comment.fg}"
assert comment.bg == "black", f"Comment bg: expected black, got {comment.bg}"

# "Arrow" is the cgdb alias for ExecutingLineArrow.
arrow = app.hl.get("Arrow")
assert arrow.fg == "bright_green", f"Arrow fg: expected bright_green, got {arrow.fg}"

# ctermbg=NONE (uppercase) must clear the colour, not store "none".
search = app.hl.get("Search")
assert search.bg is None, f"Search bg: expected None, got {search.bg}"
Style.parse(app.hl.style("Search"))
EOF
quit
