# -*- coding: utf-8 -*-
"""从 GGUF 文件里读出 chat_template 等元数据。

为什么要读它：`ollama create` 在 Modelfile 没写 TEMPLATE 时，**不会**自动用
GGUF 里的 chat_template —— 它会退化成裸的 `TEMPLATE {{ .Prompt }}`，
结果模型收到未格式化的文本，输出会乱（实测：中文提问 → 回一段俄语）。
所以要自己把官方的 chat_template 抠出来，写进 Modelfile。
"""
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")

# GGUF 值类型
_T = {
    0: "u8", 1: "i8", 2: "u16", 3: "i16", 4: "u32", 5: "i32",
    6: "f32", 7: "bool", 8: "str", 9: "arr", 10: "u64", 11: "i64", 12: "f64",
}


class R:
    def __init__(self, f):
        self.f = f

    def raw(self, n):
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError("读越界")
        return b

    def u32(self):
        return struct.unpack("<I", self.raw(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.raw(8))[0]

    def i64(self):
        return struct.unpack("<q", self.raw(8))[0]

    def s(self):
        n = self.u64()
        return self.raw(n).decode("utf-8", "replace")

    def val(self, t):
        if t == 0:
            return self.raw(1)[0]
        if t == 1:
            return struct.unpack("<b", self.raw(1))[0]
        if t == 2:
            return struct.unpack("<H", self.raw(2))[0]
        if t == 3:
            return struct.unpack("<h", self.raw(2))[0]
        if t == 4:
            return self.u32()
        if t == 5:
            return struct.unpack("<i", self.raw(4))[0]
        if t == 6:
            return struct.unpack("<f", self.raw(4))[0]
        if t == 7:
            return self.raw(1)[0] != 0
        if t == 8:
            return self.s()
        if t == 10:
            return self.u64()
        if t == 11:
            return self.i64()
        if t == 12:
            return struct.unpack("<d", self.raw(8))[0]
        if t == 9:
            et = self.u32()
            n = self.u64()
            return [self.val(et) for _ in range(n)]
        raise ValueError("未知类型 %d" % t)


def read_meta(path, want=("chat_template", "tokenizer.chat_template")):
    with open(path, "rb") as f:
        r = R(f)
        if r.raw(4) != b"GGUF":
            raise ValueError("不是 GGUF 文件")
        ver = r.u32()
        r.u64()                      # tensor count
        n_kv = r.u64()
        out = {}
        for _ in range(n_kv):
            k = r.s()
            t = r.u32()
            v = r.val(t)
            if k in want or "chat_template" in k:
                out[k] = v
            # 遇到张量数据就别往下读了（元数据都在前面）
        return ver, out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print("=" * 60)
        print(p)
        try:
            ver, meta = read_meta(p)
            print("GGUF 版本:", ver)
            if not meta:
                print("  ⚠️ 没有 chat_template 元数据")
            for k, v in meta.items():
                print("--- %s (%d 字符) ---" % (k, len(str(v))))
                print(v)
        except Exception as e:
            print("读取失败:", e)
