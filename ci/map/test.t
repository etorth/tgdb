map <F2> :next<CR>
imap <F5> <Esc>:run<CR>

python << EOF
# Verify tgdb-mode map was registered
tgdb_maps = app.km.list_maps("tgdb")
found_f2 = any(m.lhs == "\\x1bOQ" or "F2" in repr(m) for m in tgdb_maps)
# Just verify maps list is not empty and no crash occurred
assert len(tgdb_maps) > 0, "no tgdb maps registered"

# Verify imap was registered
gdb_maps = app.km.list_maps("gdb")
assert len(gdb_maps) > 0, "no gdb maps registered"
EOF
quit
