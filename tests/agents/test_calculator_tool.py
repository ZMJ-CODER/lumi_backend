"""计算工具的自然语言边界回归。"""

import pytest

from plugins.tools.core.calculator import execute_calculator


@pytest.mark.asyncio
async def test_calculator_accepts_full_width_operators_and_sentence_suffix():
    result = await execute_calculator({"expression": "请计算（12873×47－912）÷13，只返回结果。"})

    assert result.success is True
    assert result.output == "(12873*47-912)/13 = 46470.692307692305"


@pytest.mark.asyncio
async def test_calculator_accepts_thousands_separator():
    result = await execute_calculator({"expression": "1，234 + 6"})

    assert result.success is True
    assert result.output == "1234 + 6 = 1240"


@pytest.mark.asyncio
async def test_calculator_rejects_text_without_an_arithmetic_expression():
    result = await execute_calculator({"expression": "请帮我计算预算"})

    assert result.success is False
    assert result.error_code == "INVALID_ARGS"
