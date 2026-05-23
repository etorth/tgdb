set tabstop=4
set ignorecase
set hlsearch
python << EOF
cfg = app.cfg
assert cfg.tabstop == 4, f"tabstop: expected 4, got {cfg.tabstop}"
assert cfg.ignorecase is True, f"ignorecase: expected True, got {cfg.ignorecase}"
assert cfg.hlsearch is True, f"hlsearch: expected True, got {cfg.hlsearch}"
EOF
quit
