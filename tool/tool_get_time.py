"""get_time 工具：获取当前日期时间。"""

from datetime import datetime

from base_tool import Tool


class GetTimeTool(Tool):
    name = "get_time"
    description = "获取当前日期时间，无需参数"
    parameters = {"type": "object", "properties": {}}

    def execute(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
