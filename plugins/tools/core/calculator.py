"""确定性算术基础工具的纯计算实现。"""

from __future__ import annotations

import ast
import operator
import re

from app.agents.skills.base import SkillContext, ToolOutput


_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FULL_WIDTH_OPERATORS = str.maketrans({
    "×": "*", "÷": "/", "＋": "+", "－": "-", "％": "%",
    "（": "(", "）": ")",
})
_EXPRESSION_FRAGMENT = re.compile(r"[0-9.\s()+\-*/%]+")


def normalize_expression(value: object) -> str:
    """提取一段安全算术表达式，兼容模型转发的中文外壳与全角符号。"""
    raw = str(value or "").strip().translate(_FULL_WIDTH_OPERATORS)
    raw = re.sub(r"(?<=\d)[,，](?=\d)", "", raw)
    if not raw:
        return ""
    if re.fullmatch(r"[0-9.\s()+\-*/%]+", raw):
        return raw
    fragments = [item.strip() for item in _EXPRESSION_FRAGMENT.findall(raw)]
    fragments = [item for item in fragments if any(char.isdigit() for char in item)]
    if not fragments:
        return ""
    arithmetic = [item for item in fragments if any(op in item for op in "+-*/%")]
    return max(arithmetic or fragments, key=len)


def _evaluate(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if type(node.op) is ast.Pow and abs(right) > 12:
            raise ValueError("指数绝对值不能超过 12")
        value = _BINARY[type(node.op)](left, right)
        if abs(value) > 10**15:
            raise ValueError("结果超出安全范围")
        return value
    raise ValueError("仅支持数字、括号和 + - * / // % ** 运算")


async def execute_calculator(params: dict, context: SkillContext | None = None) -> ToolOutput:
    """执行受 AST 白名单约束的算术，绝不执行 Python 或用户代码。"""
    expression = normalize_expression(params.get("expression"))
    if not expression or len(expression) > 256:
        return ToolOutput(success=False, error="计算表达式为空或过长", error_code="INVALID_ARGS")
    try:
        value = _evaluate(ast.parse(expression, mode="eval"))
        precision = params.get("precision")
        if isinstance(precision, int):
            value = round(value, precision)
        return ToolOutput(
            success=True,
            output=f"{expression} = {value}",
            metadata={"decision_signals": {"result_count": 1, "confidence_hint": {"level": "high", "basis": ["deterministic_arithmetic"]}}},
        )
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
        return ToolOutput(success=False, error=f"无法计算：{exc}", error_code="INVALID_ARGS", retryable=False)
