# -*- coding: utf-8 -*-
"""生成「配置样本」`data/config.json` —— 从 backend/config.py 的 DEFAULT_CONFIG 派生。

**为什么要有它**：交付包里的 `data/config.json` 是给用户看的"可调项清单"。
以前它只躺在包里（`docker/data/` 还被 .gitignore 忽略），没有生成来源，
于是慢慢烂掉：模型名一直停在 `qwen3-vl:8b` / `qwen2.5-coder:14b`，
`num_ctx` 还写着 24576，而且漏了 7 个新增键（model_keep_alive、sd_model_dir…）。
代码有兜底值，功能不会坏；但"用户打开配置能不能看见这个项"是另一回事。

**真源永远是 `backend/config.py` 的 `DEFAULT_CONFIG`**，本脚本只负责把它抄成 JSON。

用法：
    python docker/gen_sample_config.py                    # 只写仓库内 docker/data/config.json
    python docker/gen_sample_config.py "E:/本地多模态助手-Docker"   # 顺带写入交付包
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # <repo>/docker
REPO = os.path.dirname(HERE)                               # <repo>
BACKEND = os.path.join(REPO, "backend")

sys.path.insert(0, BACKEND)
import config as cfgmod  # noqa: E402


def write(path: str, defaults: dict) -> None:
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        print("  [SKIP] 目录不存在：%s" % d)
        return
    old = None
    if os.path.isfile(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
        except Exception:
            old = None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(defaults, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if old is None:
        print("  [NEW ] %s" % path)
    else:
        changed = sorted(k for k in set(old) | set(defaults)
                         if old.get(k, "<缺>") != defaults.get(k, "<缺>"))
        print("  [UPD ] %-58s 变化 %s" % (path, ", ".join(changed) or "无"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    defaults = dict(cfgmod.DEFAULT_CONFIG)
    print("DEFAULT_CONFIG 共 %d 个键" % len(defaults))

    write(os.path.join(HERE, "data", "config.json"), defaults)

    # 交付包（可选传入包根目录）
    pkg = sys.argv[1] if len(sys.argv) > 1 else r"E:/本地多模态助手-Docker"
    if os.path.isdir(pkg):
        write(os.path.join(pkg, "data", "config.json"), defaults)
        write(os.path.join(pkg, "_deploy-kit", "data", "config.json"), defaults)
        print("\n接着：python make_deploy_kit.py （在包根目录跑）→ 重建 multimodal-deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
