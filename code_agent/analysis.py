"""Conservative, line-addressable Python AST checks (no code execution)."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    title: str
    detail: str
    suggestion: str


def analyze_source(source: str, path: str) -> dict:
    findings: list[Finding] = []

    def add(node, rule, severity, title, detail, suggestion):
        findings.append(Finding(rule, severity, path, getattr(node, "lineno", 1),
                                title, detail, suggestion))

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return {"path": path, "syntax_ok": False, "findings": [asdict(Finding(
            "syntax-error", "high", path, exc.lineno or 1, "Python 语法错误",
            exc.msg, "修复语法错误后重新运行审查。"))], "functions": []}

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{name(node.value)}.{node.attr}"
        return ""

    functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({"name": node.name, "line": node.lineno,
                              "docstring": ast.get_docstring(node) or ""})
            defaults = node.args.defaults + [x for x in node.args.kw_defaults if x is not None]
            for default in defaults:
                mutable = isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                    isinstance(default, ast.Call) and name(default.func) in {"list", "dict", "set"})
                if mutable:
                    add(default, "mutable-default", "high", "可变对象作为默认参数",
                        f"函数 {node.name} 的默认对象会在多次调用之间共享。",
                        "将默认值改为 None，并在函数内部创建新对象。")
        elif isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add(node, "bare-except", "medium", "裸 except 捕获所有异常",
                    "它会捕获 KeyboardInterrupt 和 SystemExit 等退出信号。",
                    "捕获明确的异常类型，并保留异常信息。")
            broad = node.type is None or name(node.type) in {"Exception", "BaseException"}
            if broad and all(isinstance(x, ast.Pass) for x in node.body):
                add(node, "swallowed-exception", "high", "异常被静默吞掉",
                    "失败后程序继续运行，调用者无法判断操作是否成功。",
                    "记录必要信息并重新抛出，或明确返回失败状态。")
        elif isinstance(node, ast.Call):
            called = name(node.func)
            if called in {"eval", "exec", "builtins.eval", "builtins.exec"}:
                add(node, "dynamic-execution", "high", "动态执行字符串",
                    "如果字符串受外部输入影响，可能执行任意代码；需人工确认输入来源。",
                    "使用明确的解析器、白名单操作或 ast.literal_eval 解析字面量。")
            if called.startswith("subprocess.") and any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant)
                    and k.value.value is True for k in node.keywords):
                add(node, "shell-execution", "high", "通过 shell 执行命令",
                    "若命令拼接了外部输入，可能发生命令注入；需核查调用上下文。",
                    "使用参数列表和 shell=False。")
            if called in {"pickle.load", "pickle.loads"}:
                add(node, "unsafe-deserialization", "high", "反序列化 pickle 数据",
                    "不可信 pickle 数据可执行代码；请确认数据来源。",
                    "对外部数据使用 JSON 等受限格式。")
        elif isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(comparator, ast.Constant):
                    value = comparator.value
                    if value is not None and not isinstance(value, bool):
                        add(node, "literal-identity", "medium", "用 is 比较字面量",
                            "is 比较对象身份，字符串和数字的相同值不保证是同一对象。",
                            "值比较使用 == 或 !=。")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if isinstance(node.right, ast.Constant) and node.right.value == 0:
                add(node, "zero-division", "high", "除数为常量零",
                    "执行此表达式会触发 ZeroDivisionError。", "检查除数并明确处理零值。")

    ordered = sorted(findings, key=lambda f: (f.line, f.rule))
    return {"path": path, "syntax_ok": True, "findings": [asdict(f) for f in ordered],
            "functions": sorted(functions, key=lambda f: f["line"])}
