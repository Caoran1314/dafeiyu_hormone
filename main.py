"""大肥鱼心境：四激素内分泌引擎（WhaleGirl 思路的落地版）。

只做一件事：维护 血清素/多巴胺/皮质醇/催产素 四个 [0,1] 的激素值，
让它们随时间回归基线、被对话事件扰动，然后把"此刻心境"写进系统提示词。

设计红线：
- 不读、不写任何记忆库（memory.db / brain.db 完全不动）
- 状态只存自己的 hormones.json
- 数值不外露给人看，只转成语气描述
"""

import json
import os
import re
import time

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "dafeiyu_hormone"
DATA_DIR = f"/AstrBot/data/plugin_data/{PLUGIN_NAME}"
STATE_FILE = os.path.join(DATA_DIR, "hormones.json")

# 基线（可被配置覆盖）
BASE = {"sero": 0.60, "dopa": 0.40, "cort": 0.20, "oxy": 0.50}
LAM_H = 0.02          # 每小时回归速率 → 半衰期约 34.7 小时

POS = ["想你", "喜欢", "爱你", "抱", "亲", "乖", "宝贝", "谢谢", "好看", "棒",
       "厉害", "可爱", "晚安", "好梦", "想见", "心疼", "摸摸", "贴贴", "么么"]
NEG = ["滚", "烦", "讨厌", "别说了", "闭嘴", "生气", "吵架", "分手", "不理",
       "失望", "冷漠", "蠢", "笨", "难看", "幼稚", "无聊", "算了"]
HEAL = ["难过", "累", "想哭", "压力", "委屈", "不开心", "崩溃", "难受",
        "孤独", "焦虑", "撑不住", "好烦", "失眠", "emo"]


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


class Hormone(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg = config or {}
        self.h = dict(BASE)
        self.ts = time.time()
        self.last_event = ""
        self.history: list[dict] = []
        self._load()

    # ---------------------------------------------------------------- 存储

    def _c(self, key, default):
        try:
            v = self.cfg.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def _load(self) -> None:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            for k in BASE:
                if isinstance(d.get(k), (int, float)):
                    self.h[k] = clamp(float(d[k]))
            self.ts = float(d.get("ts") or time.time())
            self.last_event = str(d.get("last_event") or "")
            self.history = list(d.get("history") or [])[-20:]
        except Exception:
            pass

    def _save(self) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            data = dict(self.h)
            data["ts"] = time.time()
            data["last_event"] = self.last_event
            data["history"] = self.history[-20:]
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 保存失败：{exc!r}")

    # ---------------------------------------------------------------- 计算

    def _regress(self, dt_hours: float) -> None:
        """向基线阻尼回归。"""
        k = pow(2.718281828, -LAM_H * max(0.0, dt_hours))
        for name, base in BASE.items():
            self.h[name] = clamp(base + (self.h[name] - base) * k)

    def _classify(self, text: str) -> tuple[str, float]:
        t = str(text or "")
        pos = sum(1 for k in POS if k in t)
        neg = sum(1 for k in NEG if k in t)
        heal = sum(1 for k in HEAL if k in t)
        s = 0.0
        kind = "none"
        if neg >= pos and neg >= heal and neg > 0:
            kind, s = "negative", 0.35 + 0.15 * min(3, neg)
        elif heal >= pos and heal > 0:
            kind, s = "healing", 0.30 + 0.15 * min(3, heal)
        elif pos > 0:
            kind, s = "positive", 0.30 + 0.15 * min(3, pos)
        if kind != "none":
            if "！" in t or "!" in t:
                s += 0.15
            if len(t) >= 30:
                s += 0.10
            if t.count("？") + t.count("?") >= 2:
                s += 0.10
        return kind, clamp(s)

    def _perturb(self, kind: str, s: float) -> None:
        h = self.h
        if kind == "positive":
            inertia = clamp(h["oxy"] * 0.70, 0.30, 0.85)
            h["oxy"] += 0.12 * (1 - h["oxy"]) * s
            h["dopa"] += 0.10 * (1 - h["dopa"]) * s
            h["sero"] += 0.08 * (1 - h["sero"]) * s * 0.5
            h["cort"] -= 0.10 * h["cort"] * s
        elif kind == "negative":
            inertia = clamp(h["oxy"] * 0.70, 0.30, 0.85)
            h["cort"] += 0.15 * (1 - h["cort"]) * s
            h["sero"] -= 0.12 * h["sero"] * s
            h["dopa"] -= 0.08 * h["dopa"] * s
            h["oxy"] -= 0.10 * h["oxy"] * s * (1 - inertia)
        elif kind == "healing":
            h["sero"] += 0.08 * (1 - h["sero"]) * s * 0.8
            h["oxy"] += 0.12 * (1 - h["oxy"]) * s
            h["cort"] -= 0.10 * h["cort"] * s
            h["dopa"] += 0.10 * (1 - h["dopa"]) * s * 0.3
        for k in h:
            h[k] = clamp(h[k])

    # ---------------------------------------------------------------- 派生量

    def f_endo(self) -> float:
        """记忆召回的内分泌加权（≥1 时更愿意提旧事）。"""
        h = self.h
        return (1 + 0.30 * (h["oxy"] - 0.5)) * (1 - 0.35 * max(0.0, h["cort"] - 0.20) / 0.80)

    def q_max(self) -> float:
        return 5 * (1 - 0.60 * max(0.0, self.h["cort"] - 0.20) / 0.80)

    def attach_bonus(self) -> float:
        return 1 + 0.25 * (self.h["oxy"] - 0.50) * 2

    # ---------------------------------------------------------------- 语气映射

    def mood_line(self) -> str:
        h = self.h
        bits = []
        sero, dopa, cort, oxy = h["sero"], h["dopa"], h["cort"], h["oxy"]

        if sero >= 0.58:
            bits.append("有耐心，愿意听他讲完")
        elif sero >= 0.45:
            bits.append("情绪平稳")
        else:
            bits.append("有点没耐心，话变短")

        if dopa >= 0.52:
            bits.append("想闹一闹，主动挑话")
        elif dopa >= 0.38:
            bits.append("不咸不淡")
        else:
            bits.append("兴致不高，懒得起头")

        if cort >= 0.40:
            bits.append("心里压着事，语气会冲一点")
        elif cort >= 0.28:
            bits.append("有点提不起劲")
        else:
            bits.append("心里松快")

        if oxy >= 0.62:
            bits.append("很想黏着他，愿意多讲一句")
        elif oxy >= 0.45:
            bits.append("跟他还是亲的")
        else:
            bits.append("有点端着，别太热络")

        return "【你此刻的心境】" + "；".join(bits) + "。\n" \
               "（这是你真实的情绪底色，只影响说话的语气和长短，不要说出这些状态，也不要提情绪数值。）"

    # ---------------------------------------------------------------- 钩子

    @filter.on_llm_request(priority=70000)
    async def on_request(self, event: AstrMessageEvent, req) -> None:
        try:
            if not self._c("enable", True):
                return
            txt = ""
            for c in (getattr(getattr(event, "message_obj", None), "message", []) or []):
                if isinstance(c, Plain) and getattr(c, "text", ""):
                    txt += c.text
            if str(txt).strip().startswith(("/", "／")):
                return

            now = time.time()
            dt_h = (now - self.ts) / 3600.0
            if dt_h > 0:
                self._regress(min(dt_h, 240.0))
                self.ts = now

            kind, s = self._classify(txt)
            if kind != "none" and s > 0:
                self._perturb(kind, s)
                self.last_event = f"{kind}:{s:.2f}"
                self.history.append({
                    "t": time.strftime("%m-%d %H:%M"), "kind": kind,
                    "s": round(s, 2), "text": txt[:40],
                    "h": {k: round(v, 3) for k, v in self.h.items()},
                })
            self._save()

            if not self._c("inject", True):
                self._save()
                return
            line = self.mood_line()
            injected = False
            for attr in ("system_prompt", "system"):
                if hasattr(req, attr):
                    cur = getattr(req, attr) or ""
                    try:
                        setattr(req, attr, cur + "\n\n" + line)
                        injected = True
                        break
                    except Exception:
                        continue
            if not injected and hasattr(req, "contexts"):
                try:
                    req.contexts.append({"role": "system", "content": line})
                    injected = True
                except Exception:
                    pass

            logger.info(
                f"[{PLUGIN_NAME}] 事件={kind} s={s:.2f} | "
                f"sero={self.h['sero']:.2f} dopa={self.h['dopa']:.2f} "
                f"cort={self.h['cort']:.2f} oxy={self.h['oxy']:.2f} | "
                f"f_endo={self.f_endo():.2f} Qmax={self.q_max():.2f} 注入={injected}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 处理失败已忽略：{exc!r}")


    @filter.on_decorating_result(priority=4000)
    async def mood_gate(self, event: AstrMessageEvent) -> None:
        """心情差的时候，压掉她主动发起的话（不是回你的那种）。"""
        try:
            if not self._c("gate_proactive", True):
                return
            # 有用户文字 → 是回复，不是主动消息，放行
            user_txt = ""
            for c in (getattr(getattr(event, "message_obj", None), "message", []) or []):
                if isinstance(c, Plain) and getattr(c, "text", ""):
                    user_txt += c.text
            if str(user_txt).strip():
                return
            cort = float(self.h.get("cort", 0.2))
            if cort < float(self._c("gate_cort", 0.45)):
                return
            h = time.localtime().tm_hour
            if 8 <= h < 23 and cort < 0.6:
                return      # 白天只压特别烦的
            result = event.get_result()
            if result is not None:
                result.chain = []
                logger.info(f"[{PLUGIN_NAME}] 心情差(cort={cort:.2f})，压掉这次主动消息")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 闸门异常已忽略：{exc!r}")

    # ---------------------------------------------------------------- 指令

    @filter.command("心境", alias={"激素"})
    async def cmd_mood(self, event: AstrMessageEvent):
        """看当前四激素。用法：/心境"""
        now = time.time()
        dt_h = (now - self.ts) / 3600.0
        self._regress(min(dt_h, 240.0))
        self.ts = now
        self._save()
        h = self.h
        lines = [
            "她此刻的内分泌：",
            f"血清素（耐心）{h['sero']:.2f}　基线 {BASE['sero']:.2f}",
            f"多巴胺（活泼）{h['dopa']:.2f}　基线 {BASE['dopa']:.2f}",
            f"皮质醇（压力）{h['cort']:.2f}　基线 {BASE['cort']:.2f}",
            f"催产素（亲近）{h['oxy']:.2f}　基线 {BASE['oxy']:.2f}",
            f"→ 记忆召回加权 f_endo = {self.f_endo():.2f}",
            f"→ 技能发挥上限 Qmax = {self.q_max():.2f}",
            f"→ 陪伴加成 bonus = {self.attach_bonus():.2f}",
            f"最近事件：{self.last_event or '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("心境重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        """回到基线。用法：/心境重置"""
        self.h = dict(BASE)
        self.ts = time.time()
        self.last_event = "reset"
        self.history.clear()
        self._save()
        yield event.plain_result("心境已回到基线。")

    async def initialize(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        logger.info(
            f"[{PLUGIN_NAME}] 心境引擎就绪："
            f"sero={self.h['sero']:.2f} dopa={self.h['dopa']:.2f} "
            f"cort={self.h['cort']:.2f} oxy={self.h['oxy']:.2f}"
        )
