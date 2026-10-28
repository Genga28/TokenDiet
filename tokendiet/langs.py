"""Language tables and the tree-sitter extractor (definitions, calls, identifier refs, imports)."""

import re
from dataclasses import dataclass, field

EXTRACTOR_VERSION = "3"  # bump when extraction changes so existing indexes are rebuilt

EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".java": "java", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".scala": "scala",
}

DEF_RE = re.compile(
    r"(?:^|_)(class|function|method|interface|struct|enum|trait|type|module|impl|constructor|record|object|protocol)"
    r"_(declaration|definition|item|spec|specifier)$"
)
EXTRA_DEFS = {"method": "method", "singleton_method": "method", "class": "class", "module": "module"}  # ruby
SPECIFIER_NEEDS_BODY = {"struct_specifier", "class_specifier", "enum_specifier", "union_specifier"}

CALL_TYPES = {
    "call", "call_expression", "method_invocation", "invocation_expression", "new_expression",
    "object_creation_expression", "function_call_expression", "member_call_expression",
}
CALLEE_FIELDS = ("function", "method", "constructor", "name", "type")
NAME_FIELDS = ("property", "attribute", "field", "name")
IDENT_TYPES = {"identifier", "type_identifier", "constant", "simple_identifier", "property_identifier",
               "field_identifier", "name"}
REF_TYPES = {"identifier", "type_identifier", "constant", "simple_identifier"}
FUNC_VALUE_TYPES = {"arrow_function", "function", "function_expression", "generator_function"}


@dataclass
class Sym:
    name: str
    kind: str
    start: int  # 1-based lines
    end: int
    signature: str
    parent: int = -1  # index into symbols list
    depth: int = 0


@dataclass
class FileFacts:
    symbols: list = field(default_factory=list)
    calls: list = field(default_factory=list)  # (callee_name, line, enclosing symbol index or -1)
    refs: dict = field(default_factory=dict)  # name -> count
    imports: list = field(default_factory=list)  # raw specifier strings


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _ident_name(node, src):
    """Reduce an expression to its trailing identifier: a.b.c() -> c, new Foo<T>() -> Foo."""
    for _ in range(12):
        if node is None:
            return None
        if node.type in IDENT_TYPES or node.type.endswith("identifier"):
            return _text(node, src)
        nxt = None
        for f in NAME_FIELDS:
            nxt = node.child_by_field_name(f)
            if nxt is not None:
                break
        if nxt is None:
            named = [c for c in node.named_children if c.type not in ("type_arguments", "arguments", "argument_list")]
            nxt = named[-1] if named else None
        node = nxt
    return None


def _def_name(node, src):
    n = node.child_by_field_name("name") or node.child_by_field_name("left") or node.child_by_field_name("key")
    if n is not None:
        return _ident_name(n, src) or _text(n, src)
    d = node.child_by_field_name("declarator")  # C/C++ function_definition
    while d is not None:
        if d.type in IDENT_TYPES or d.type.endswith("identifier"):
            return _text(d, src)
        nd = d.child_by_field_name("declarator") or d.child_by_field_name("name")
        if nd is None:
            return _ident_name(d, src)
        d = nd
    t = node.child_by_field_name("type")  # rust impl_item
    if t is not None:
        return _text(t, src)
    for c in node.named_children:
        if c.type in IDENT_TYPES:
            return _text(c, src)
    return None


def _signature(node, src) -> str:
    raw = src[node.start_byte:min(node.end_byte, node.start_byte + 400)].decode("utf-8", "replace")
    lines = raw.splitlines() or [""]
    sig = lines[0].strip()
    # keep multi-line parameter lists short: join until parens balance (max 4 lines)
    i = 1
    while sig.count("(") > sig.count(")") and i < min(4, len(lines)):
        sig += " " + lines[i].strip()
        i += 1
    sig = sig.rstrip("{").rstrip()
    return sig[:160]


def _strip_quotes(s: str) -> str:
    return s.strip().strip("'\"`<>")


def _imports_of(node, src, lang, out):
    t = node.type
    if lang == "python":
        if t == "import_statement":
            for c in node.named_children:
                tgt = c.child_by_field_name("name") if c.type == "aliased_import" else c
                if tgt is not None:
                    out.append(_text(tgt, src))
        elif t == "import_from_statement":
            m = node.child_by_field_name("module_name")
            if m is not None:
                mod = _text(m, src)
                names = [c for c in node.children_by_field_name("name")]
                out.append(mod)
                for n in names:  # `from pkg import submodule` may name a module
                    nm = n.child_by_field_name("name") if n.type == "aliased_import" else n
                    if nm is not None:
                        out.append(mod.rstrip(".") + "." + _text(nm, src) if not mod.endswith(".") else mod + _text(nm, src))
    elif lang in ("javascript", "typescript", "tsx"):
        if t in ("import_statement", "export_statement"):
            s = node.child_by_field_name("source")
            if s is not None:
                out.append(_strip_quotes(_text(s, src)))
        elif t == "call_expression":
            f = node.child_by_field_name("function")
            if f is not None and _text(f, src) in ("require", "import"):
                a = node.child_by_field_name("arguments")
                if a is not None and a.named_children and a.named_children[0].type in ("string", "template_string"):
                    out.append(_strip_quotes(_text(a.named_children[0], src)))
    elif lang == "go" and t == "import_spec":
        p = node.child_by_field_name("path")
        if p is not None:
            out.append(_strip_quotes(_text(p, src)))
    elif lang in ("java", "kotlin", "scala") and t in ("import_declaration", "import_header"):
        out.append(re.sub(r"^import\s+(static\s+)?|;$|\.\*$", "", _text(node, src).strip()).strip())
    elif lang == "rust":
        if t == "use_declaration":
            a = node.child_by_field_name("argument")
            if a is not None:
                out.append(_text(a, src))
        elif t == "mod_item" and node.child_by_field_name("body") is None:
            n = node.child_by_field_name("name")
            if n is not None:
                out.append("mod:" + _text(n, src))
    elif lang in ("c", "cpp") and t == "preproc_include":
        p = node.child_by_field_name("path")
        if p is not None and p.type == "string_literal":
            out.append(_strip_quotes(_text(p, src)))
    elif lang == "csharp" and t == "using_directive":
        out.append(re.sub(r"^using\s+(static\s+)?|;$", "", _text(node, src).strip()).strip())
    elif lang == "ruby" and t == "call":
        m = node.child_by_field_name("method")
        if m is not None and _text(m, src) in ("require", "require_relative"):
            a = node.child_by_field_name("arguments")
            if a is not None and a.named_children:
                spec = _strip_quotes(_text(a.named_children[0], src))
                out.append(("./" + spec) if _text(m, src) == "require_relative" else spec)


def extract(tree, src: bytes, lang: str) -> FileFacts:
    facts = FileFacts()
    # iterative DFS keeping the enclosing-definition stack
    stack = [(tree.root_node, -1, 0)]
    while stack:
        node, enclosing, depth = stack.pop()
        t = node.type
        sym_idx = enclosing
        kind = None
        m = DEF_RE.search(t)
        if m and not (t in SPECIFIER_NEEDS_BODY and node.child_by_field_name("body") is None):
            kind = m.group(1)
        elif lang == "ruby" and t in EXTRA_DEFS:
            kind = EXTRA_DEFS[t]
        elif t == "variable_declarator" and lang in ("javascript", "typescript", "tsx"):
            v = node.child_by_field_name("value")
            if v is not None and v.type in FUNC_VALUE_TYPES:
                kind = "function"
        elif t in ("assignment_expression", "pair") and lang in ("javascript", "typescript", "tsx"):
            # res.send = function send() {}, module.exports.x = () => {}, { handler: function () {} }
            v = node.child_by_field_name("right") or node.child_by_field_name("value")
            if v is not None and v.type in FUNC_VALUE_TYPES:
                kind = "function"
        if kind:
            name = _def_name(node, src)
            if name and len(name) < 200:
                if kind == "function" and enclosing >= 0 and facts.symbols[enclosing].kind in ("class", "interface", "struct", "impl", "trait"):
                    kind = "method"
                sig_node = node.parent if t in ("variable_declarator", "assignment_expression") and node.parent is not None else node
                facts.symbols.append(Sym(name, kind, node.start_point[0] + 1, node.end_point[0] + 1,
                                         _signature(sig_node, src), enclosing,
                                         facts.symbols[enclosing].depth + 1 if enclosing >= 0 else 0))
                sym_idx = len(facts.symbols) - 1
        if t in CALL_TYPES:
            callee = None
            for f in CALLEE_FIELDS:
                c = node.child_by_field_name(f)
                if c is not None:
                    callee = _ident_name(c, src)
                    break
            if callee and len(callee) < 120:
                facts.calls.append((callee, node.start_point[0] + 1, enclosing if not kind else sym_idx))
                facts.refs[callee] = facts.refs.get(callee, 0) + 1
        elif t in REF_TYPES or t == "property_identifier":
            nm = _text(node, src)
            # z.ZodType / models.User: namespace member access to a type-like name counts as a reference
            if 2 < len(nm) < 120 and (t != "property_identifier" or nm[0].isupper() or nm[0] == "$"):
                facts.refs[nm] = facts.refs.get(nm, 0) + 1
        _imports_of(node, src, lang, facts.imports)
        children = node.children
        for c in reversed(children):
            if c.child_count or c.type in REF_TYPES or c.type == "property_identifier":
                stack.append((c, sym_idx, depth + 1))
    # a definition's own name is not a reference to itself
    for s in facts.symbols:
        if s.name in facts.refs:
            facts.refs[s.name] -= 1
            if facts.refs[s.name] <= 0:
                del facts.refs[s.name]
    return facts
