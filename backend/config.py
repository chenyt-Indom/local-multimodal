# -*- coding: utf-8 -*-
"""全局配置：本地多模态助手
所有可在界面中调整的参数集中在此，便于“包括环境与参数”的定制需求。
"""
import json
import os
import sys

# 项目根 / 后端包目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)


def is_frozen() -> bool:
    """是否处于 PyInstaller 打包运行状态。"""
    return bool(getattr(sys, "frozen", False))


def res_root() -> str:
    """只读打包资源根（打包后为 _MEIPASS/_internal；源码运行时为项目根）。
    frontend、SD 模型权重等只读资源都放这里。"""
    return getattr(sys, "_MEIPASS", PROJECT_ROOT)


def data_root() -> str:
    """可写运行数据根。

    优先级：环境变量 MM_DATA_DIR > 打包后 exe 同级目录 > 源码项目根。
    config.json、data/ 记忆与会话等运行时数据放这里。
    Docker 部署时设置 MM_DATA_DIR=/data，即可只挂载一个卷实现全部数据持久化。
    """
    env = os.environ.get("MM_DATA_DIR")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    return os.path.dirname(sys.executable) if is_frozen() else PROJECT_ROOT


def res(*parts: str) -> str:
    """拼只读资源路径。"""
    return os.path.join(res_root(), *parts)


def data(*parts: str) -> str:
    """拼可写数据路径，并确保父目录存在。"""
    path = os.path.join(data_root(), *parts)
    os.makedirs(os.path.dirname(path) or data_root(), exist_ok=True)
    return path


CONFIG_FILE = data("config.json")

DEFAULT_CONFIG = {
    # Ollama 本地服务地址（请保证 ollama serve 已启动）
    "ollama_url": "http://127.0.0.1:11434",
    # 默认模型，需先用 ollama pull qwen3-vl:8b 下载
    "default_model": "qwen3-vl:8b",
    # 推理参数
    "temperature": 0.7,
    # 上下文窗口（token 数）。**这个值必须在所有模型调用里保持一致** ——
    # Ollama 只要发现与已加载的不同，就会卸载重载模型（实测约 5 秒），
    # 而重载会中断正在进行的生成，表现为"模型加载一半、思考一半、没有回答"。
    #
    # 预算说明（实测）：工具 schema ~3200-4300 token + 系统提示 ~1000 token，
    # 固定开销就有 5000 左右。8192 时只剩 3000 给历史+问题+思考+回答，太紧；
    # 提到 12288 后约剩 7000，实测显存占用 ~10.6GB/12.2GB，仍有余量。
    # 显存更小的机器可以调回 8192（Ollama 会自动降级到 CPU，只是变慢）。
    "num_ctx": 12288,
    # Qwen3-VL 无法关闭思考，思考会先吃掉大量 token。实测 2048 时常出现
    # 「思考到一半就断、正文一个字都没写」（Ollama 会返回 done_reason=length）。
    # 提到 4096 后基本能「想完 + 写完」；主流程另有截断自动重试兜底。
    "max_tokens": 4096,
    # —— 能力开关（前端控制）——
    "rag_enabled": False,        # 知识库检索增强生成开关
    "web_enabled": False,        # 联网搜索开关（默认关闭，打开才会联网）
    "memory_enabled": True,      # 长期记忆开关
    "auto_memorize": True,       # 对话后自动提取重要内容存入短期记忆
    # 联网搜索每轮最多注入结果条数
    "web_top_k": 4,
    # RAG 每轮最多检索文档数
    "rag_top_k": 4,
    # 记忆每轮最多注入条数
    "memory_top_k": 5,
    # 可调用的外部 API 工具（前端设置界面可配置）
    "api_tools": [],
    # 绘图（文生图/图片微改）使用的计算设备：
    #   "auto" = 自动（有可用显卡就用显卡）
    #   "cpu"  = 强制 CPU
    #   "gpu"  = 强制显卡（无显卡时切换会被拒绝，不会静默回退）
    "t2i_device": "auto",
    # 程序启动时自动打开麦克风监听（说「小千小千」唤醒）。
    # 容器里没有麦克风，会自动跳过并提示，不影响其它功能。
    "voice_auto_start": True,
}


def load_config() -> dict:
    """读取配置文件；不存在则写入默认配置。

    环境变量可覆盖个别关键项（容器化部署用，未设置时行为不变）：
      MM_OLLAMA_URL  → ollama_url（精简版镜像里 Ollama 是独立容器，需指向服务名）
      MM_MODEL       → default_model
    """
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            cfg.update(user)
        except Exception:
            pass
    if os.environ.get("MM_OLLAMA_URL"):
        cfg["ollama_url"] = os.environ["MM_OLLAMA_URL"]
    if os.environ.get("MM_MODEL"):
        cfg["default_model"] = os.environ["MM_MODEL"]

    # ⚠️ 把 localhost 换成 127.0.0.1。
    # Windows 上 localhost 会先解析到 IPv6 的 ::1，而 Ollama 只监听 IPv4，
    # 于是每次连接都要先等一次回退超时 —— **实测 2065ms，换成 127.0.0.1 只需 1ms**。
    # 模型调用很频繁，这一项每年要白等掉大量时间，所以在这里统一兜住：
    # 即使老配置里还写着 localhost 也会被自动纠正。
    url = str(cfg.get("ollama_url") or "")
    if "localhost" in url:
        cfg["ollama_url"] = url.replace("localhost", "127.0.0.1")
    return cfg


def save_config(cfg: dict) -> None:
    """把用户修改后的配置写回文件，实现参数持久化。"""
    clean = {k: cfg.get(k) for k in DEFAULT_CONFIG}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)