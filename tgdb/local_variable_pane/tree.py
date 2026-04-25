"""Backward-compat shim — tree expansion helpers moved to tgdb.varobj_tree."""

from ..varobj_tree.tree import VarobjTreeMixin as LocalVariablePaneTreeMixin

__all__ = ["LocalVariablePaneTreeMixin"]
