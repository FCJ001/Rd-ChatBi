# ============================================================
# Prompt 文件加载器
# ============================================================

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """读取 prompts/<name>.prompt 文件内容（进程内缓存：prompt 是只读资源，
    没必要每个请求读一次盘；改 prompt 后重启生效）"""
    prompt_path = Path(__file__).parents[2] / "prompts" / f"{name}.prompt"
    return prompt_path.read_text(encoding="utf-8")
