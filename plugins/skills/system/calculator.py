"""安全的确定性算术 Skill，不执行 Python 或任意表达式。"""

from __future__ import annotations

import ast
import operator

from app.agents.skills.base import Skill, SkillContext, SkillResult


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


class CalculatorSkill(Skill):
    name = "calculator"
    description = "精确计算数字、括号、加减乘除、百分比换算后的算术表达式；不会联网或执行代码。"
    category = "system"
    environment = "server"
    scenes = ["chat", "office", "game"]
    domain = "system"
    intent_tags = ["计算", "算一下", "算术", "加减乘除", "百分比", "表达式"]
    use_when = ["用户要求精确算术、百分比或括号表达式计算"]
    do_not_use_when = ["仅需解释数学概念", "需要统计附件或表格数据时先读取数据"]
    selection_examples = ["“帮我算一下 (12873*47-912)/13” → 使用"]
    result_contract = "返回表达式和确定性计算结果。"
    deterministic = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "仅含数字、空格、括号和 + - * / // % ** 的表达式；百分号请先转换为小数。",
            },
            "precision": {"type": "integer", "minimum": 0, "maximum": 12, "description": "可选小数位数"},
        },
        "required": ["expression"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        expression = str(params.get("expression") or "").strip().replace("×", "*").replace("÷", "/")
        if not expression or len(expression) > 256:
            return SkillResult(False, error="计算表达式为空或过长", error_code="INVALID_ARGS")
        try:
            tree = ast.parse(expression, mode="eval")
            value = _evaluate(tree)
            precision = params.get("precision")
            if isinstance(precision, int):
                value = round(value, precision)
            return SkillResult(
                success=True,
                output=f"{expression} = {value}",
                metadata={"decision_signals": {"result_count": 1, "confidence_hint": {"level": "high", "basis": ["deterministic_arithmetic"]}}},
            )
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
            return SkillResult(False, error=f"无法计算：{exc}", error_code="INVALID_ARGS", retryable=False)
