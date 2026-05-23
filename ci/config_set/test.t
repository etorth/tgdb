set tabstop=4
set ignorecase
set hlsearch
python << EOF
cfg = app.cfg
assert cfg.tabstop == 4, f"tabstop: expected 4, got {cfg.tabstop}"
assert cfg.ignorecase is True, f"ignorecase: expected True, got {cfg.ignorecase}"
assert cfg.hlsearch is True, f"hlsearch: expected True, got {cfg.hlsearch}"
assert app.target_width == 82, f"target_width: expected 82, got {app.target_width}"
assert app.target_height == 31, f"target_height: expected 31, got {app.target_height}"
EOF
quit
