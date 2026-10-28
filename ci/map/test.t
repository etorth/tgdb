map <F2> :next<CR>
imap <F5> <Esc>:run<CR>

python << EOF
# Verify the tgdb-mode map and the gdb-mode imap were registered
assert app.km._roots["tgdb"].children, "no tgdb maps registered"
assert app.km._roots["gdb"].children, "no gdb maps registered"

# Unmapping must remove them again
app.km._buf["tgdb"].clear()
tokens = list(app.km._roots["tgdb"].children)
assert len(tokens) == 1, f"expected one tgdb map, got {tokens}"
assert app.km.unmap("tgdb", tokens), "unmap of the tgdb map failed"
assert not app.km._roots["tgdb"].children, "tgdb map still present after unmap"
EOF
quit
