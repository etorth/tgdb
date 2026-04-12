"""
Public entry point for the local-variable-pane package.

External code should import :class:`LocalVariablePane` from
``tgdb.local_variable_pane`` and treat the sibling modules in this package as
implementation details. The package is intentionally split by responsibility,
but the public interface is still the single ``LocalVariablePane`` widget.

Typical usage::

    pane = LocalVariablePane(hl, cfg)
    pane.set_var_callbacks(
        var_create=...,
        var_list_children=...,
        var_delete=...,
        var_update=...,
        var_eval=...,
        var_eval_expr=...,
        get_decl_lines=...,
    )
    pane.set_variables(locals_snapshot, current_frame)

After those dependencies are provided, the pane behaves like a black-box:
callers push debugger state in, while the widget owns lazy loading, expansion
restore, shadowed-variable handling, and varobj lifecycle management.
"""

from .pane import LocalVariablePane

__all__ = ["LocalVariablePane"]
