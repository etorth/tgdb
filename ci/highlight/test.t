highlight Comment ctermfg=Blue ctermbg=Black
highlight Arrow ctermfg=Green

python << EOF
from rich.color import Color

comment_style = app.hl.get_style("Comment")
arrow_style = app.hl.get_style("Arrow")

assert comment_style is not None, "Comment highlight not set"
assert comment_style.color == Color.parse("blue"), f"Comment fg: expected blue, got {comment_style.color}"
assert comment_style.bgcolor == Color.parse("black"), f"Comment bg: expected black, got {comment_style.bgcolor}"
assert arrow_style is not None, "Arrow highlight not set"
assert arrow_style.color == Color.parse("green"), f"Arrow fg: expected green, got {arrow_style.color}"
EOF
quit
