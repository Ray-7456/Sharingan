"""规则文件中的公式求值：受限语法 + 函数白名单，**绝不使用 eval/exec**。

规则文件是用户可以手改的文件，因此公式必须当作不可信输入处理。

支持：

- 数字字面量：``4.1`` / ``.5`` / ``85``；
- 列引用：``H`` / ``AB`` / ``O4`` / ``$B$4``（行号与 ``$`` 被忽略，因为模板里
  的引用是相对当前行的）；
- 运算符：``+ - * /``、括号、一元正负号；
- 函数白名单：``abs()``。

一律报错（不猜测、不降级）：区域引用（``B4:B1029``）、白名单外的函数、
属性访问、下划线标识符。

缺失值传播：任一被引用列的值为 ``None`` 时，结果为 ``None``。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Mapping

_ALLOWED_FUNCTIONS = frozenset({"abs"})

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d*)?|\.\d+)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>[+\-*/()]))"
)
_REF_RE = re.compile(r"^([A-Za-z]{1,2})(\d*)$")


class FormulaError(ValueError):
    """公式语法错误或求值失败。"""


def _tokenize(source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(source):
        match = _TOKEN_RE.match(source, position)
        if match is None:
            if source[position:].strip() == "":
                break
            raise FormulaError(f"无法识别的字符 {source[position]!r}（位置 {position}）")
        position = match.end()
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.index = 0

    def peek(self) -> tuple[str | None, str | None]:
        if self.index < len(self.tokens):
            return self.tokens[self.index]
        return None, None

    def advance(self) -> tuple[str | None, str | None]:
        token = self.peek()
        self.index += 1
        return token

    def expect_close_paren(self) -> None:
        if self.peek() != ("op", ")"):
            raise FormulaError("括号不匹配：缺少 ')'")
        self.advance()

    def parse(self) -> tuple:
        node = self.expression()
        if self.peek()[0] is not None:
            raise FormulaError(f"表达式存在多余内容：{self.peek()[1]!r}")
        return node

    def expression(self) -> tuple:
        node = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            operator = self.advance()[1]
            node = ("bin", operator, node, self.term())
        return node

    def term(self) -> tuple:
        node = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            operator = self.advance()[1]
            node = ("bin", operator, node, self.unary())
        return node

    def unary(self) -> tuple:
        if self.peek() == ("op", "-"):
            self.advance()
            return ("neg", self.unary())
        if self.peek() == ("op", "+"):
            self.advance()
            return self.unary()
        return self.primary()

    def primary(self) -> tuple:
        kind, value = self.peek()
        if kind == "num":
            self.advance()
            return ("num", float(value))
        if kind == "ident":
            self.advance()
            assert value is not None
            if self.peek() == ("op", "("):
                self.advance()
                argument = self.expression()
                self.expect_close_paren()
                name = value.lower()
                if name not in _ALLOWED_FUNCTIONS:
                    raise FormulaError(
                        f"不允许的函数 {value!r}；白名单：{sorted(_ALLOWED_FUNCTIONS)}"
                    )
                return ("call", name, argument)
            reference = _REF_RE.match(value)
            if reference is None:
                raise FormulaError(f"非法列引用 {value!r}（应为 1-2 个字母，可带行号）")
            return ("ref", reference.group(1).upper())
        if kind == "op" and value == "(":
            self.advance()
            node = self.expression()
            self.expect_close_paren()
            return node
        raise FormulaError("表达式不完整或语法错误")


@lru_cache(maxsize=512)
def parse(text: str) -> tuple:
    """解析公式文本，返回 AST；带缓存。"""
    if not isinstance(text, str):
        raise FormulaError(f"公式必须是字符串，收到 {type(text).__name__}")
    source = text.strip()
    if source.startswith("="):
        source = source[1:]
    source = source.replace("$", "")
    if not source.strip():
        raise FormulaError("公式为空")
    return _Parser(_tokenize(source)).parse()


def _collect_references(node: tuple, out: set[str]) -> None:
    kind = node[0]
    if kind == "ref":
        out.add(node[1])
    elif kind == "neg":
        _collect_references(node[1], out)
    elif kind == "bin":
        _collect_references(node[2], out)
        _collect_references(node[3], out)
    elif kind == "call":
        _collect_references(node[2], out)


@lru_cache(maxsize=512)
def referenced(text: str) -> frozenset[str]:
    """返回公式引用到的列字母集合。"""
    out: set[str] = set()
    _collect_references(parse(text), out)
    return frozenset(out)


def _evaluate_node(node: tuple, values: Mapping[str, float | None]) -> float | None:
    kind = node[0]
    if kind == "num":
        return float(node[1])
    if kind == "ref":
        value = values.get(node[1])
        return None if value is None else float(value)
    if kind == "neg":
        inner = _evaluate_node(node[1], values)
        return None if inner is None else -inner
    if kind == "bin":
        left = _evaluate_node(node[2], values)
        right = _evaluate_node(node[3], values)
        if left is None or right is None:
            return None
        operator = node[1]
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if right == 0:
            raise FormulaError("除数为 0")
        return left / right
    if kind == "call":  # 白名单内目前只有 abs
        inner = _evaluate_node(node[2], values)
        return None if inner is None else abs(inner)
    raise FormulaError(f"未知节点类型：{node!r}")


def evaluate(text: str, values: Mapping[str, float | None]) -> float | None:
    """按给定列值求值；任一引用为缺失值时返回 ``None``。"""
    return _evaluate_node(parse(text), values)


def check(text: str) -> None:
    """只做语法检查，合法则返回，否则抛 :class:`FormulaError`。"""
    parse(text)
