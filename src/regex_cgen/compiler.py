"""Compile regex patterns to minimised DFA.

Pipeline: regex string → sre_parse AST → Thompson NFA → subset-construction
DFA → Hopcroft minimisation → renumbered DFA with explicit dead state.
"""

from __future__ import annotations

import re
import sre_parse
from collections import deque

try:
    from sre_constants import (
        ANY,
        AT,
        BRANCH,
        CATEGORY,
        CATEGORY_DIGIT,
        CATEGORY_NOT_DIGIT,
        CATEGORY_NOT_SPACE,
        CATEGORY_NOT_WORD,
        CATEGORY_SPACE,
        CATEGORY_WORD,
        IN,
        LITERAL,
        MAX_REPEAT,
        MIN_REPEAT,
        NEGATE,
        NOT_LITERAL,
        RANGE,
        SUBPATTERN,
    )
except ImportError:
    from re._constants import (  # Python ≥ 3.13
        ANY,
        AT,
        BRANCH,
        CATEGORY,
        CATEGORY_DIGIT,
        CATEGORY_NOT_DIGIT,
        CATEGORY_NOT_SPACE,
        CATEGORY_NOT_WORD,
        CATEGORY_SPACE,
        CATEGORY_WORD,
        IN,
        LITERAL,
        MAX_REPEAT,
        MIN_REPEAT,
        NEGATE,
        NOT_LITERAL,
        RANGE,
        SUBPATTERN,
    )

MAXREPEAT = sre_parse.MAXREPEAT

# Limit to prevent runaway DFA construction
_MAX_DFA_STATES = 10_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIGIT = frozenset(range(48, 58))  # '0'-'9'
_WORD = (
    frozenset(range(48, 58))
    | frozenset(range(65, 91))
    | frozenset(range(97, 123))
    | frozenset({95})
)
_SPACE = frozenset({9, 10, 12, 13, 32})


def _category_bytes(cat: int) -> frozenset[int]:
    if cat == CATEGORY_DIGIT:
        return _DIGIT
    if cat == CATEGORY_NOT_DIGIT:
        return frozenset(range(256)) - _DIGIT
    if cat == CATEGORY_WORD:
        return _WORD
    if cat == CATEGORY_NOT_WORD:
        return frozenset(range(256)) - _WORD
    if cat == CATEGORY_SPACE:
        return _SPACE
    if cat == CATEGORY_NOT_SPACE:
        return frozenset(range(256)) - _SPACE
    return frozenset()


# ---------------------------------------------------------------------------
# NFA builder (Thompson construction)
# ---------------------------------------------------------------------------

class NFABuilder:
    """Build a Thompson NFA from an ``sre_parse`` AST."""

    def __init__(self, *, case_insensitive: bool = False, dot_all: bool = False):
        self._next: int = 0
        self.transitions: dict[tuple[int, int], set[int]] = {}
        self.epsilon: dict[int, set[int]] = {}
        self.case_insensitive = case_insensitive
        self.dot_all = dot_all

    # -- state helpers -------------------------------------------------------

    def _new(self) -> int:
        s = self._next
        self._next += 1
        return s

    def _tr(self, src: int, byte: int, dst: int) -> None:
        self.transitions.setdefault((src, byte), set()).add(dst)

    def _eps(self, src: int, dst: int) -> None:
        self.epsilon.setdefault(src, set()).add(dst)

    # -- character helpers ---------------------------------------------------

    def _add_char(self, src: int, code: int, dst: int) -> None:
        """Add transition(s) for a single character code point."""
        if code <= 127:
            self._tr(src, code, dst)
            if self.case_insensitive and chr(code).isalpha():
                self._tr(src, ord(chr(code).swapcase()), dst)
        else:
            # Encode as UTF-8 byte sequence
            utf8 = chr(code).encode("utf-8")
            prev = src
            for i, b in enumerate(utf8):
                nxt = dst if i == len(utf8) - 1 else self._new()
                self._tr(prev, b, nxt)
                prev = nxt
            if self.case_insensitive:
                other = chr(code).swapcase()
                if other != chr(code):
                    other_utf8 = other.encode("utf-8")
                    prev = src
                    for i, b in enumerate(other_utf8):
                        nxt = dst if i == len(other_utf8) - 1 else self._new()
                        self._tr(prev, b, nxt)
                        prev = nxt

    def _char_set(self, items: list) -> set[int]:
        """Resolve an ``IN`` item list to a set of **byte** values (0-255).

        Only ASCII code-points are included; non-ASCII code-points are
        returned separately via ``_non_ascii_set``.
        """
        chars: set[int] = set()
        negate = False
        for op, value in items:
            if op == NEGATE:
                negate = True
            elif op == LITERAL:
                if value <= 127:
                    chars.add(value)
                    if self.case_insensitive and chr(value).isalpha():
                        chars.add(ord(chr(value).swapcase()))
            elif op == RANGE:
                lo, hi = value
                for c in range(lo, min(hi + 1, 128)):
                    chars.add(c)
                    if self.case_insensitive and chr(c).isalpha():
                        chars.add(ord(chr(c).swapcase()))
            elif op == CATEGORY:
                chars |= set(_category_bytes(value))
        if negate:
            chars = set(range(256)) - chars
        return chars

    def _non_ascii_codes(self, items: list) -> set[int]:
        """Collect non-ASCII code-points from an ``IN`` item list."""
        codes: set[int] = set()
        negate = False
        for op, value in items:
            if op == NEGATE:
                negate = True
            elif op == LITERAL:
                if value > 127:
                    codes.add(value)
            elif op == RANGE:
                lo, hi = value
                # Cap expansion to prevent explosion
                for c in range(max(lo, 128), min(hi + 1, 0x800)):
                    codes.add(c)
        # Negation of non-ASCII is too complex for byte-level DFA; skip
        if negate:
            return set()
        return codes

    # -- fragment builders ---------------------------------------------------

    def build(self, parsed) -> tuple[int, int]:
        return self._seq(list(parsed))

    def _seq(self, items: list) -> tuple[int, int]:
        if not items:
            s0, s1 = self._new(), self._new()
            self._eps(s0, s1)
            return s0, s1
        frag = self._elem(items[0])
        for item in items[1:]:
            nxt = self._elem(item)
            self._eps(frag[1], nxt[0])
            frag = (frag[0], nxt[1])
        return frag

    def _elem(self, item) -> tuple[int, int]:
        op, val = item
        if op == LITERAL:
            return self._literal(val)
        if op == NOT_LITERAL:
            return self._not_literal(val)
        if op == ANY:
            return self._any()
        if op == IN:
            return self._in(val)
        if op == BRANCH:
            return self._branch(val)
        if op == SUBPATTERN:
            return self._subpat(val)
        if op in (MAX_REPEAT, MIN_REPEAT):
            return self._repeat(val)
        if op == AT:
            # Anchors are no-ops for fullmatch
            s0, s1 = self._new(), self._new()
            self._eps(s0, s1)
            return s0, s1
        raise ValueError(f"Unsupported regex op: {op}")

    def _literal(self, code: int) -> tuple[int, int]:
        s0, s1 = self._new(), self._new()
        self._add_char(s0, code, s1)
        return s0, s1

    def _not_literal(self, code: int) -> tuple[int, int]:
        s0, s1 = self._new(), self._new()
        if code <= 127:
            excluded = {code}
            if self.case_insensitive and chr(code).isalpha():
                excluded.add(ord(chr(code).swapcase()))
            for b in range(256):
                if b not in excluded:
                    self._tr(s0, b, s1)
        else:
            # Approximate: accept all bytes
            for b in range(256):
                self._tr(s0, b, s1)
        return s0, s1

    def _any(self) -> tuple[int, int]:
        s0, s1 = self._new(), self._new()
        for b in range(256):
            if self.dot_all or b != 10:  # '\n' == 10
                self._tr(s0, b, s1)
        return s0, s1

    def _in(self, items: list) -> tuple[int, int]:
        s0, s1 = self._new(), self._new()
        for b in self._char_set(items):
            self._tr(s0, b, s1)
        for code in self._non_ascii_codes(items):
            self._add_char(s0, code, s1)
        return s0, s1

    def _branch(self, val) -> tuple[int, int]:
        _, branches = val
        s0, s1 = self._new(), self._new()
        for branch in branches:
            frag = self._seq(branch)
            self._eps(s0, frag[0])
            self._eps(frag[1], s1)
        return s0, s1

    def _subpat(self, val) -> tuple[int, int]:
        _gid, _add, _del, pattern = val
        return self._seq(pattern)

    def _repeat(self, val) -> tuple[int, int]:
        lo, hi, pattern = val
        if lo == 0 and hi == MAXREPEAT:
            return self._star(pattern)
        if lo == 1 and hi == MAXREPEAT:
            return self._plus(pattern)
        if lo == 0 and hi == 1:
            return self._opt(pattern)
        return self._bounded(pattern, lo, hi)

    def _star(self, pattern) -> tuple[int, int]:
        s0, s1 = self._new(), self._new()
        f = self._seq(pattern)
        self._eps(s0, f[0])
        self._eps(s0, s1)
        self._eps(f[1], f[0])
        self._eps(f[1], s1)
        return s0, s1

    def _plus(self, pattern) -> tuple[int, int]:
        s0, s1 = self._new(), self._new()
        f = self._seq(pattern)
        self._eps(s0, f[0])
        self._eps(f[1], f[0])
        self._eps(f[1], s1)
        return s0, s1

    def _opt(self, pattern) -> tuple[int, int]:
        s0, s1 = self._new(), self._new()
        f = self._seq(pattern)
        self._eps(s0, f[0])
        self._eps(s0, s1)
        self._eps(f[1], s1)
        return s0, s1

    def _bounded(self, pattern, lo: int, hi: int) -> tuple[int, int]:
        frags = []
        for _ in range(lo):
            frags.append(self._seq(pattern))
        if hi == MAXREPEAT:
            frags.append(self._star(pattern))
        else:
            for _ in range(hi - lo):
                frags.append(self._opt(pattern))
        if not frags:
            s0, s1 = self._new(), self._new()
            self._eps(s0, s1)
            return s0, s1
        result = frags[0]
        for f in frags[1:]:
            self._eps(result[1], f[0])
            result = (result[0], f[1])
        return result


# ---------------------------------------------------------------------------
# NFA → DFA (subset construction)
# ---------------------------------------------------------------------------

def _epsilon_closure(states: set[int], eps: dict[int, set[int]]) -> frozenset[int]:
    closure = set(states)
    stack = list(states)
    while stack:
        s = stack.pop()
        for t in eps.get(s, ()):
            if t not in closure:
                closure.add(t)
                stack.append(t)
    return frozenset(closure)


def _nfa_to_dfa(
    builder: NFABuilder, start: int, accept: int
) -> dict:
    # Pre-compute per-state outgoing bytes for fast lookup
    state_bytes: dict[int, set[int]] = {}
    for (src, b) in builder.transitions:
        state_bytes.setdefault(src, set()).add(b)

    initial = _epsilon_closure({start}, builder.epsilon)

    dfa_map: dict[frozenset[int], int] = {initial: 0}
    dfa_trans: dict[tuple[int, int], int] = {}
    dfa_accept: set[int] = set()

    if accept in initial:
        dfa_accept.add(0)

    counter = 1
    queue: deque[frozenset[int]] = deque([initial])

    while queue:
        cur = queue.popleft()
        cur_id = dfa_map[cur]

        # Collect all bytes that have at least one transition from *cur*
        active_bytes: set[int] = set()
        for s in cur:
            if s in state_bytes:
                active_bytes |= state_bytes[s]

        for b in active_bytes:
            nxt_nfa: set[int] = set()
            for s in cur:
                nxt_nfa |= builder.transitions.get((s, b), set())
            if not nxt_nfa:
                continue
            nxt_closure = _epsilon_closure(nxt_nfa, builder.epsilon)
            if nxt_closure not in dfa_map:
                if counter >= _MAX_DFA_STATES:
                    raise ValueError(
                        f"DFA state limit exceeded ({_MAX_DFA_STATES})"
                    )
                dfa_map[nxt_closure] = counter
                counter += 1
                queue.append(nxt_closure)
                if accept in nxt_closure:
                    dfa_accept.add(dfa_map[nxt_closure])
            dfa_trans[(cur_id, b)] = dfa_map[nxt_closure]

    return {
        "num_states": counter,
        "initial": 0,
        "accept": dfa_accept,
        "transitions": dfa_trans,
    }


# ---------------------------------------------------------------------------
# DFA helpers
# ---------------------------------------------------------------------------

def _add_dead_state(dfa: dict) -> dict:
    """Ensure every (state, byte) pair has a transition.

    Missing transitions are redirected to an explicit *dead state* that
    loops to itself on every byte.
    """
    n = dfa["num_states"]
    trans = dict(dfa["transitions"])
    dead = n
    needs_dead = False

    for s in range(n):
        for b in range(256):
            if (s, b) not in trans:
                trans[(s, b)] = dead
                needs_dead = True

    if not needs_dead:
        return dfa

    for b in range(256):
        trans[(dead, b)] = dead

    return {
        "num_states": dead + 1,
        "initial": dfa["initial"],
        "accept": dfa["accept"],
        "transitions": trans,
    }


def _minimize_dfa(dfa: dict) -> dict:
    """Hopcroft's DFA minimisation."""
    n = dfa["num_states"]
    accept = dfa["accept"]
    trans = dfa["transitions"]
    initial = dfa["initial"]

    if n <= 1:
        return dfa

    # Pre-compute reverse transitions: (byte, target) -> set of sources
    reverse: dict[tuple[int, int], set[int]] = {}
    for (s, b), t in trans.items():
        reverse.setdefault((b, t), set()).add(s)

    accept_s = set(accept)
    non_accept_s = set(range(n)) - accept_s

    P: list[set[int]] = []
    if accept_s:
        P.append(accept_s)
    if non_accept_s:
        P.append(non_accept_s)

    W: list[set[int]] = [set(s) for s in P]

    while W:
        A = W.pop()
        for c in range(256):
            # X = states that transition to some state in A on byte c
            X: set[int] = set()
            for a in A:
                X |= reverse.get((c, a), set())
            if not X:
                continue

            new_P: list[set[int]] = []
            for Y in P:
                inter = Y & X
                diff = Y - X
                if inter and diff:
                    new_P.append(inter)
                    new_P.append(diff)
                    if Y in W:
                        W.remove(Y)
                        W.append(inter)
                        W.append(diff)
                    else:
                        W.append(inter if len(inter) <= len(diff) else diff)
                else:
                    new_P.append(Y)
            P = new_P

    state_map: dict[int, int] = {}
    for i, part in enumerate(P):
        for s in part:
            state_map[s] = i

    new_trans: dict[tuple[int, int], int] = {}
    new_accept: set[int] = set()
    for (s, b), t in trans.items():
        new_trans[(state_map[s], b)] = state_map[t]
    for s in accept:
        new_accept.add(state_map[s])

    return {
        "num_states": len(P),
        "initial": state_map[initial],
        "accept": new_accept,
        "transitions": new_trans,
    }


def _renumber(dfa: dict) -> dict:
    """Renumber so that dead-state = 0 and initial-state = 1."""
    n = dfa["num_states"]
    trans = dfa["transitions"]
    accept = dfa["accept"]
    initial = dfa["initial"]

    # Find dead state: non-accepting, all self-loops
    dead = None
    for s in range(n):
        if s in accept:
            continue
        if all(trans.get((s, b), s) == s for b in range(256)):
            dead = s
            break

    mapping: dict[int, int] = {}
    nxt = 0
    if dead is not None:
        mapping[dead] = 0
        nxt = 1
    if initial not in mapping:
        mapping[initial] = nxt
        nxt += 1
    for s in range(n):
        if s not in mapping:
            mapping[s] = nxt
            nxt += 1

    new_trans: dict[tuple[int, int], int] = {}
    new_accept: set[int] = set()
    for (s, b), t in trans.items():
        new_trans[(mapping[s], b)] = mapping[t]
    for s in accept:
        new_accept.add(mapping[s])

    return {
        "num_states": n,
        "initial": mapping[initial],
        "accept": new_accept,
        "transitions": new_trans,
    }


# ---------------------------------------------------------------------------
# Pre-processing
# ---------------------------------------------------------------------------

def _preprocess_pattern(pattern: str) -> str:
    r"""Convert PCRE2-style ``\x{NNNN}`` escapes to literal characters."""
    result: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 3] == "\\x{":
            end = pattern.find("}", i + 3)
            if end != -1:
                hex_str = pattern[i + 3 : end]
                try:
                    code = int(hex_str, 16)
                    if code <= 0x10FFFF:
                        result.append(chr(code))
                        i = end + 1
                        continue
                except ValueError:
                    pass
        result.append(pattern[i])
        i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compile_regex(pattern: str, flags: str = "") -> dict:
    """Compile *pattern* to a minimised, renumbered DFA.

    Returns a dict with ``num_states``, ``initial``, ``accept``, and
    ``transitions`` keys.
    """
    re_flags = 0
    ci = False
    da = False
    for f in flags:
        if f == "i":
            ci = True
            re_flags |= re.IGNORECASE
        elif f == "s":
            da = True
            re_flags |= re.DOTALL
        elif f == "m":
            re_flags |= re.MULTILINE
        # Note: 'x' (verbose) is intentionally NOT mapped to re.VERBOSE.
        # re2 does not support verbose mode; the flag is a PCRE2 test
        # directive that we ignore.

    pattern = _preprocess_pattern(pattern)
    parsed = sre_parse.parse(pattern, re_flags)

    builder = NFABuilder(case_insensitive=ci, dot_all=da)
    start, accept = builder.build(parsed)

    dfa = _nfa_to_dfa(builder, start, accept)
    dfa = _add_dead_state(dfa)
    dfa = _minimize_dfa(dfa)
    dfa = _renumber(dfa)
    return dfa
