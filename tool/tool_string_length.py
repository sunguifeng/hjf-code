"""string_length 工具：计算字符串长度。"""

from base_tool import Tool


class StringLengthTool(Tool):
    name = "string_length"
    description = "计算字符串长度"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "任意文本"},
        },
        "required": ["text"],
    }

    def execute(self, text: str) -> str:
        return str(len(text))
