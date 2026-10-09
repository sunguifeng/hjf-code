"""ls 工具：列出目录结构，输出缩进树（只读）。

流水线：参数解析 -> 路径解析 -> 遍历收集 -> 建树 -> 打印。
"""

import fnmatch
import os
from pathlib import Path

from base_tool import Tool

# 相对路径的起点：项目根目录（tool/ 的上一级）
_ROOT = Path(__file__).resolve().parent.parent

# 最多收集的条目数，超过即截断
MAX_LS_FILES = 1000

# 内置黑名单：常见依赖目录 / 构建产物（按名字匹配，命中整棵子树剪掉）
BLACKLIST_DIRS = {
    "__pycache__", "node_modules", "dist", "build", "target",
    "vendor", "bin", "obj",
}

# 内置黑名单：常见产物文件（glob 匹配 base 名）
BLACKLIST_PATTERNS = ["*.pyc", "*.so", "*.dll", "*.exe", "*.class", "*.o"]


def _resolve(path: str) -> str:
    """空路径回退项目根目录；相对路径拼上项目根目录变成绝对路径。"""
    p = Path(path or ".")
    if not p.is_absolute():
        p = _ROOT / p
    return str(p)


def _should_skip(base: str, ignore: list) -> bool:
    """过滤规则，按顺序判断三组规则，命中任一即跳过（整棵子树剪掉）。"""
    # 1. 隐藏项：base 名以 . 开头（.git、.idea、.venv 等）
    if base.startswith("."):
        return True
    # 2. 内置黑名单
    if base in BLACKLIST_DIRS:
        return True
    if any(fnmatch.fnmatch(base, pat) for pat in BLACKLIST_PATTERNS):
        return True
    # 3. 用户 ignore：对 base 名做 glob 匹配
    if any(fnmatch.fnmatch(base, pat) for pat in ignore):
        return True
    return False


def _list_directory(root: str, ignore: list) -> tuple:
    """深度优先遍历（每层按字典序），返回 (扁平路径列表, 是否截断)。

    目录路径以 os.sep 结尾——这是建树时区分文件/目录的唯一标记。
    """
    entries = []
    truncated = False

    def walk(rel):
        nonlocal truncated
        full = os.path.join(root, rel) if rel else root
        try:
            names = sorted(os.listdir(full))      # 每层字典序，输出稳定
        except OSError:
            return                                # 无权限的目录整棵跳过
        for name in names:
            if _should_skip(name, ignore):
                continue
            child = f"{rel}{os.sep}{name}" if rel else name
            if os.path.isdir(os.path.join(root, child)):
                entries.append(child + os.sep)     # 目录打尾部标记
                if len(entries) >= MAX_LS_FILES:
                    truncated = True
                    return
                walk(child)                        # 下钻（剪枝 = 不递归即剪子树）
            else:
                entries.append(child)
                if len(entries) >= MAX_LS_FILES:
                    truncated = True
                    return

    walk("")
    return entries, truncated


def _create_file_tree(entries: list) -> dict:
    """扁平路径列表 -> 嵌套树。

    节点表示：目录 -> dict，文件 -> None。
    同一个前缀只创建一次；中间段一定是目录，
    最后一段看原始路径是否以 os.sep 结尾来区分目录/文件。
    """
    tree = {}
    for rel in entries:
        is_dir = rel.endswith(os.sep)
        parts = rel.rstrip(os.sep).split(os.sep)
        node = tree
        for part in parts[:-1]:                   # 中间段：已有节点直接下钻
            node = node.setdefault(part, {})
        leaf = parts[-1]
        if is_dir:
            if not isinstance(node.get(leaf), dict):
                node[leaf] = {}                   # 目录
        elif leaf not in node:
            node[leaf] = None                     # 文件
    return tree


def _print_tree(root_label: str, tree: dict) -> list:
    """渲染缩进树：根一行，每层缩进 2 空格，目录名追加 os.sep。"""
    lines = [f"- {root_label}{os.sep}"]

    def print_node(name, node, depth):
        indent = "  " * depth
        if node is None:
            lines.append(f"{indent}- {name}")
        else:
            lines.append(f"{indent}- {name}{os.sep}")
            for child in node:                    # 遍历序 = 收集序（字典序）
                print_node(child, node[child], depth + 1)

    for name in tree:
        print_node(name, tree[name], 1)
    return lines


class LsDirTool(Tool):
    name = "ls"
    description = (
        "只读工具：列出目录结构，输出缩进树，隐藏项（. 开头）和常见构建产物"
        "（__pycache__、node_modules、dist、build、*.pyc 等）自动忽略。"
        "path 为目录路径，相对路径以项目根目录为起点，默认列出项目根目录；"
        "ignore 为额外的忽略 glob 模式列表（按文件/目录名匹配）。"
        "超过 1000 个条目时截断并在输出开头提示。"
        "示例：列出项目根目录 -> path='.'；列出 tool 目录并忽略 python 文件 -> "
        "path='tool', ignore=['*.py']。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "目录路径：相对路径以项目根目录为起点，"
                                    "默认 '.'（项目根目录）"},
            "ignore": {"type": "array", "items": {"type": "string"},
                       "description": "额外忽略的 glob 模式列表，如 ['*.log', 'docs']"},
        },
        "required": [],
    }

    def execute(self, path: str = ".", ignore: list = None) -> str:
        # 1. 参数解析（结构不对走框架的 ERROR 转文本路径）
        if ignore is None:
            ignore = []
        if not isinstance(ignore, list) or not all(isinstance(p, str) for p in ignore):
            raise ValueError(f"ignore 必须是字符串列表: {ignore!r}")

        # 2. 路径规整 + 3. 存在性检查
        root = _resolve(path)
        if not os.path.exists(root):
            raise FileNotFoundError(f"路径不存在: {path}")
        if os.path.isfile(root):                  # 传了文件：直接返回它自己
            return f"- {os.path.basename(root)}"

        # 4. 遍历收集
        entries, truncated = _list_directory(root, ignore)

        # 5. 建树 + 6. 打印
        lines = _print_tree(root, _create_file_tree(entries))

        # 收尾：截断提示 + 元数据
        header = []
        if truncated:
            header.append(f"（超过 {MAX_LS_FILES} 个条目已截断，"
                          f"建议用更精确的路径或用 ignore 参数缩小范围）")
        header.append(f"（共 {len(entries)} 个条目，truncated={str(truncated).lower()}）")
        return "\n".join(header + lines)


if __name__ == "__main__":
    sep = os.sep
    tool = LsDirTool()

    # 默认：项目根目录（隐藏目录 .venv/.git 应被剪掉）
    out = tool.execute(".", [])
    assert f"- agent{sep}" in out and f"- tool{sep}" in out, "根目录树应含子目录"
    assert ".venv" not in out and ".git" not in out, "隐藏项应被忽略"
    print("默认根目录     OK:\n" + out + "\n")

    # 指定目录
    out = tool.execute("tool", [])
    assert "- base_tool.py" in out and "tool_read_range.py" in out, "tool 目录应列出工具文件"
    print("指定目录       OK:\n" + out + "\n")

    # 用户 ignore：剪掉所有 tool_*.py
    out = tool.execute("tool", ["tool_*"])
    assert "tool_read_range.py" not in out, "ignore 模式应生效"
    assert "- base_tool.py" in out, "未匹配 ignore 的文件应保留"
    print("ignore 过滤    OK:\n" + out + "\n")

    # 传文件路径
    out = tool.execute("main.py", [])
    assert out == "- main.py", "文件路径应只返回它自己"
    print("文件路径       OK:", out)

    # 不存在路径 -> FileNotFoundError（框架会转 ERROR 头文本）
    try:
        tool.execute("no_such_dir", [])
    except FileNotFoundError:
        print("不存在路径     OK: FileNotFoundError（框架转 ERROR 头文本）")
    else:
        raise AssertionError("不存在的路径应抛 FileNotFoundError")

    print("\n全部测试通过")
