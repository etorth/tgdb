"""
Public implementation of the register-pane package.

``RegisterPane`` is a black-box widget for showing named register values. The
caller constructs it once, then pushes parsed register snapshots through
``set_registers(...)`` whenever the active frame changes.

Registers are grouped into named categories (General, Segment, FPU, SSE/AVX,
…) and rendered as an expandable tree so the long list of vector / FPU
registers does not crowd out commonly inspected GPRs.  Expansion state and
the value-changed highlight survive across snapshots.
"""

import re

from rich.text import Text
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from ..gdb_controller import RegisterInfo
from ..highlight_groups import HighlightGroups
from ..pane_base import PaneBase


_CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("General", re.compile(
        r"^("
        # x86_64 GPRs and their narrower aliases
        r"r[abcd]x|rsi|rdi|rbp|rsp|r8|r9|r1[0-5]|rip|"
        r"e[abcd]x|esi|edi|ebp|esp|eip|"
        # x86 flags
        r"eflags|rflags|flags|"
        # ARM/AArch64 GPRs
        r"x[0-9]+|w[0-9]+|"
        r"sp|pc|lr|fp|cpsr|spsr|xpsr|psr|primask|faultmask|basepri|control|"
        # 32-bit ARM r0..r15 (intentionally separate from r8-r15 above so
        # AArch64 / x86_64 both match without false positives)
        r"r[0-9]|r1[0-5]"
        r")$", re.IGNORECASE)),
    ("Segment", re.compile(
        r"^(cs|ss|ds|es|fs|gs|fs_base|gs_base)$", re.IGNORECASE)),
    ("FPU", re.compile(
        r"^(st[0-7]|fctrl|fstat|ftag|fiseg|fioff|foseg|fooff|fop)$",
        re.IGNORECASE)),
    ("SSE/AVX", re.compile(
        r"^(xmm[0-9]+|ymm[0-9]+|zmm[0-9]+|mxcsr)$", re.IGNORECASE)),
    ("Vector", re.compile(
        # AArch64 SIMD/FP registers (Neon): v0..v31, q0..q31, d0..d31, s0..s31,
        # plus the FP control/status registers fpcr/fpsr.
        r"^(v[0-9]+|q[0-9]+|d[0-9]+|s[0-9]+|fpcr|fpsr)$", re.IGNORECASE)),
    ("SVE", re.compile(
        # AArch64 Scalable Vector Extension: z0..z31, p0..p15, ffr.
        r"^(z[0-9]+|p[0-9]+|ffr)$", re.IGNORECASE)),
    ("MMX", re.compile(r"^mm[0-9]+$", re.IGNORECASE)),
    ("Mask", re.compile(r"^k[0-7]$", re.IGNORECASE)),
]


_CATEGORY_ORDER = [
    "General", "Segment", "FPU", "SSE/AVX", "Vector", "SVE", "MMX", "Mask",
    "Other",
]


_DEFAULT_EXPANDED = {"General"}


def _categorize(name: str) -> str:
    """Return the group name for *register-name*."""
    for group_name, regex in _CATEGORY_RULES:
        if regex.match(name):
            return group_name
    return "Other"


class _RegisterTree(Tree):
    """Tree widget for registers; highlights values that just changed."""

    def render_label(self, node, base_style, style) -> Text:
        text = super().render_label(node, base_style, style)
        data = node.data
        if isinstance(data, dict) and data.get("changed"):
            text.stylize("yellow")
        return text


class RegisterPane(PaneBase):
    """Render register values for the active frame, grouped by category.

    Public interface
    ----------------
    ``RegisterPane(hl, **kwargs)``
        Create the widget.

    ``set_registers(registers)``
        Replace the visible register snapshot.  Expansion state of category
        nodes is preserved across calls; values that changed since the last
        snapshot are briefly highlighted.
    """

    DEFAULT_CSS = """
    RegisterPane > Tree {
        width: 1fr;
        height: 1fr;
        background: $surface;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(hl, **kwargs)
        self._registers: list[RegisterInfo] = []
        self._previous_values: dict[str, str] = {}
        self._expanded: set[str] = set(_DEFAULT_EXPANDED)


    def title(self) -> str:
        return "REGISTERS"


    def compose(self):
        yield from super().compose()
        yield _RegisterTree("")


    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.show_root = False
        tree.root.expand()
        self._rebuild_tree()


    def set_registers(self, registers: list[RegisterInfo]) -> None:
        """Publish the latest named-register snapshot."""
        self._registers = list(registers)
        if self.is_mounted:
            self._rebuild_tree()


    def _rebuild_tree(self) -> None:
        try:
            tree = self.query_one(Tree)
        except Exception:
            return

        # Save current expansion state of category nodes before rebuild.
        for node in list(tree.root.children):
            label_plain = (
                node.label.plain if hasattr(node.label, "plain") else str(node.label)
            )
            group_name = label_plain.split(" ", 1)[0]
            if node.is_expanded:
                self._expanded.add(group_name)
            else:
                self._expanded.discard(group_name)

        tree.clear()

        # Bucket registers by category, preserving GDB's emission order.
        buckets: dict[str, list[RegisterInfo]] = {name: [] for name in _CATEGORY_ORDER}
        for register in self._registers:
            buckets[_categorize(register.name)].append(register)

        new_values: dict[str, str] = {}
        for group_name in _CATEGORY_ORDER:
            entries = buckets.get(group_name) or []
            if not entries:
                continue
            label = f"{group_name}  ({len(entries)})"
            group_node: TreeNode = tree.root.add(label, expand=False)
            if group_name in self._expanded:
                group_node.expand()
            for register in entries:
                changed = (
                    register.name in self._previous_values
                    and self._previous_values[register.name] != register.value
                )
                child_label = self._format_register(register)
                group_node.add_leaf(
                    child_label,
                    data={"name": register.name, "changed": changed},
                )
                new_values[register.name] = register.value
        self._previous_values = new_values


    @staticmethod
    def _format_register(register: RegisterInfo) -> str:
        return f"{register.name} = {register.value}"
