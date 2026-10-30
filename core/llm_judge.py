"""把「疑似 / 缺失」条目交给大模型复判：这条到底算已提供，还是真缺。

只做一件事：给定清单要求的一条文件、实际交付的候选文件与同目录文件、正文摘录，
判断它属于「已提供（并卷 / 异名 / 本就不由施工单位归档）」「真缺」还是「无法判断」。

三条硬约束：
1. **只给建议，不改判定**。改判仍走 OverrideStore 的人工通道，人工点采纳才生效；
2. **quote 必须能在摘录里找到**，找不到就降级为「无法判断」——防幻觉的硬闸；
3. **结果按 prompt 哈希缓存**，同一批数据重跑 0 费用。

协议只支持两种：OpenAI 兼容（/chat/completions，覆盖 DeepSeek / 通义 / Ollama）
和 Anthropic 原生（/messages）。不新增依赖，HTTP 用 requests。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # 项目其余部分都依赖 requests（streamlit 自带），这里只做兜底
    import requests  # type: ignore
except Exception:  # pragma: no cover - 极少见
    requests = None  # type: ignore

VERDICT_LABELS = {"match": "建议已提供", "gap": "建议真缺", "unclear": "无法判断"}
VERDICTS = frozenset(VERDICT_LABELS)

PRESETS: dict[str, dict[str, str]] = {
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "style": "openai"},
    "Claude": {"base_url": "https://api.anthropic.com/v1", "model": "claude-sonnet-5-5", "style": "anthropic"},
    "本地 Ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b", "style": "openai"},
    "自定义": {"base_url": "", "model": "", "style": "openai"},
}

SYSTEM_PROMPT = (
    "你是电网工程竣工档案的资料员。用户会给你一条归档清单要求，"
    "以及实际交付的候选文件、案卷卷内文件、以及候选文件的正文摘录。\n"
    "请判断：清单要求的这份文件，在交付的档案里是否已经提供。\n"
    "判定口径（按顺序）：\n"
    "1. **先判归档责任**，看【归档责任】里的「立卷单位」和「清单来源」两行：\n"
    "   清单来源 = 这份文件由谁出具；立卷单位 = 谁负责把它组卷。\n"
    "   **两行里只要出现「建设」「监理」「设计」「地方政府」等其它单位中的任何一个**，"
    "就说明这份文件由对方出具或由对方归档。对方的档案你看不到，所以这一类"
    "**不要判「真缺」**——判 match（basis=duty，quote 抄那句立卷/来源原文），"
    "并在 reason 里写明由谁归档；\n"
    "   两行都只写着「施工」或「相关」时，才轮到下面按文件找——"
    "**这时「立卷单位：施工」不能拿来证明文件已提供**，责任在施工单位就必须真的找到文件，"
    "找不到就是「真缺」；\n"
    "2. 责任在施工单位时，再看交付的文件：同一卷里另有文件能证明这份内容（并卷）、"
    "同一份文件的不同写法（异名）—— 都算「已提供」；\n"
    "3. 责任在施工单位、确实找不到 —— 「真缺」；\n"
    "4. 依据不足以下结论 —— 「无法判断」。宁可说无法判断，也不要把不确定说成确定。\n"
    "只能依据给出的材料判断，不要脑补没给出的信息。"
    "作答必须是一段 JSON，不要任何解释文字。"
)

OUTPUT_SPEC = (
    "只输出 JSON，形如：\n"
    '{"verdict":"match|gap|unclear","confidence":0.0,"reason":"不超过 40 字",'
    '"basis":"file|duty","quote":"支撑结论的原文片段，不超过 40 字"}\n'
    "verdict 只能是 match（已提供）、gap（真缺）、unclear（无法判断）。\n"
    "判 match 时必须用 basis 说明依据：basis=file 表示凭交付的文件判断，"
    "quote 逐字抄【正文摘录】或文件名；basis=duty 表示凭【归档责任】判断，"
    "quote 逐字抄【归档责任】里的原文（如「建设 / 施工」之类）。不要改写、不要拼接。\n"
    "判 gap、unclear 时 basis 留空、quote 留空字符串。"
)

# 两段式的第一段：只问「哪几卷里可能有」，不让它直接下结论。
# 原因：一次把全部案卷名和全部文件名塞给模型，上下文太长、判断反而糊；
# 而人复档案时也是先翻案卷目录挑卷、再翻那一卷的文件。
SHORTLIST_SYSTEM = (
    "你是电网工程竣工档案的资料员。先做检索，不做结论。\n"
    "用户会给你一条归档清单要求，以及这次交付的全部案卷名（括号里是该卷文件数）。\n"
    "请指出哪几卷里最可能装着这份文件，依据只能是案卷名里的编号段、专业和文件类型。\n"
    "即使【归档责任】显示这份由建设单位、监理或设计单位出具，也要指出可能收着它的那一卷"
    "（例如竣工验收卷、设计变更卷）；只有整份目录确实与它无关时才返回空数组。\n"
    "作答必须是一段 JSON，不要任何解释文字。"
)

def _cases_spec(limit: int = 4) -> str:
    return (
        "只输出 JSON，形如：\n"
        '{"cases":["案卷名1","案卷名2"],"reason":"不超过 40 字"}\n'
        "cases 必须逐字照抄上面案卷清单里的案卷名（不要带括号里的文件数），"
        f"最多 {limit} 个，按可能性从高到低排；"
        "确实没有哪一卷可能装这份文件就填 []，不要硬凑。"
    )


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class LLMConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    style: str = "openai"  # openai | anthropic
    timeout: float = 60.0
    retries: int = 3
    workers: int = 4
    max_excerpt: int = 800
    temperature: float = 0.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """从环境变量读配置，key 不落盘。

        LLM_BASE_URL / LLM_MODEL / LLM_API_KEY / LLM_STYLE；
        key 没设 LLM_API_KEY 时依次回落到 DEEPSEEK_API_KEY、ANTHROPIC_API_KEY。
        """
        env = os.environ
        key = env.get("LLM_API_KEY") or env.get("DEEPSEEK_API_KEY") or env.get("ANTHROPIC_API_KEY") or ""
        style = env.get("LLM_STYLE", "")
        if not style:
            style = "anthropic" if env.get("ANTHROPIC_API_KEY") and not env.get("LLM_API_KEY") else "openai"
        default = PRESETS["Claude" if style == "anthropic" else "DeepSeek"]
        return cls(
            base_url=env.get("LLM_BASE_URL") or default["base_url"],
            model=env.get("LLM_MODEL") or default["model"],
            api_key=key,
            style=style,
        )

    @property
    def ready(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip() and self.api_key.strip())

    @property
    def missing(self) -> str:
        gaps = [
            name
            for name, value in (
                ("地址 base_url", self.base_url),
                ("模型 model", self.model),
                ("密钥 api_key", self.api_key),
            )
            if not str(value).strip()
        ]
        return "、".join(gaps)


# --------------------------------------------------------------------------
# 判定结果
# --------------------------------------------------------------------------
@dataclass
class LLMVerdict:
    verdict: str = "unclear"
    confidence: float = 0.0
    reason: str = ""
    quote: str = ""
    cached: bool = False
    error: str = ""

    @property
    def label(self) -> str:
        base = VERDICT_LABELS.get(self.verdict, "无法判断")
        if self.confidence:
            return f"{base}（{self.confidence:.2f}）"
        return base

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            "quote": self.quote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], cached: bool = False) -> "LLMVerdict":
        return cls(
            verdict=str(data.get("verdict", "unclear")),
            confidence=float(data.get("confidence", 0) or 0),
            reason=str(data.get("reason", "")),
            quote=str(data.get("quote", "")),
            cached=cached,
        )


@dataclass
class LLMAnswer:
    """一次请求的原文，解析之前的形态（两段式的第一段只要原文）。"""
    raw: str = ""
    error: str = ""
    cached: bool = False


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------
def related_names(text: str, files: Iterable[Any], limit: int = 12, min_score: float = 0.3) -> list[str]:
    """在整个资料目录里按文件名检索与清单条目最接近的文件。

    人工判「异名 / 放错文件夹」时是翻整个目录的；只给同分类号目录会把这种证据挡在外面。
    这里用 2-gram 重合度做廉价检索，命中的文件名与原条目文本越像排得越前。
    """
    target = _grams(text or "")
    if not target:
        return []
    scored: list[tuple[float, str, str]] = []
    for record in files:
        rel = str(getattr(record, "rel_path", ""))
        name = Path(rel).name
        grams = _grams(name)
        if not grams:
            continue
        overlap = len(target & grams) / len(target)
        if overlap >= min_score:
            scored.append((overlap, name, rel))
    scored.sort(key=lambda item: (-item[0], item[2]))
    out: list[str] = []
    for _, name, _ in scored:
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def related_folders(
    text: str, files: Iterable[Any], limit: int = 8, per_folder: int = 5, min_score: float = 0.15
) -> list[tuple[str, list[str]]]:
    """按案卷（文件夹）检索：返回 [(案卷名, [其中文件名…])]。

    竣工图这类条目，文件名里根本没有分类号，只有案卷名能说明「这一卷里装的是什么」。
    人工判「并卷 / 放错文件夹」靠的就是先看案卷名，所以这里也按案卷名匹配。
    """
    target = _grams(text or "")
    if not target:
        return []
    folders: dict[str, list[str]] = {}
    for record in files:
        rel = str(getattr(record, "rel_path", ""))
        parent = str(Path(rel).parent.as_posix())
        folders.setdefault(parent, []).append(Path(rel).name)

    scored: list[tuple[float, str]] = []
    for parent in folders:
        label = parent.rsplit("/", 1)[-1] if parent != "." else ""
        grams = _grams(label)
        if not grams:
            continue
        score = len(target & grams) / len(target)
        if score >= min_score:
            scored.append((score, parent))
    scored.sort(key=lambda item: (-item[0], item[1]))

    out: list[tuple[str, list[str]]] = []
    for _, parent in scored[:limit]:
        label = parent.rsplit("/", 1)[-1] if parent != "." else "（根目录）"
        out.append((label, folders[parent][:per_folder]))
    return out


def case_list(files: Iterable[Any], limit: int = 150) -> tuple[list[tuple[str, int]], bool]:
    """全目录的案卷清单：[(案卷名, 文件数)]，以及是否被截断。

    人工复核的第一步是「这个分类号该有的东西，哪些案卷里可能有」——看的正是案卷名。
    图纸类条目和案卷名没有共同关键词，词面检索帮不上忙，只能把清单摊开给模型看。
    案卷太多（> limit）时才截断，退化为按词面检索。
    """
    counts: dict[str, int] = {}
    for record in files:
        rel = str(getattr(record, "rel_path", ""))
        parent = Path(rel).parent.as_posix()
        label = parent.rsplit("/", 1)[-1] if parent != "." else "（根目录）"
        counts[label] = counts.get(label, 0) + 1
    items = sorted(counts.items(), key=lambda item: item[0])
    return items[:limit], len(items) > limit


def case_index(files: Iterable[Any], limit: int = 150) -> tuple[dict[str, list[str]], bool]:
    """案卷名 → 卷内文件名。两段式复判的第二段要用它把选中的卷摊开给模型看。

    返回 (索引, 是否截断)。键的顺序按案卷名排，和 case_list 保持一致。
    """
    folders: dict[str, list[str]] = {}
    for record in files:
        rel = str(getattr(record, "rel_path", ""))
        parent = Path(rel).parent.as_posix()
        label = parent.rsplit("/", 1)[-1] if parent != "." else "（根目录）"
        folders.setdefault(label, []).append(Path(rel).name)
    names = sorted(folders)[:limit]
    return {name: folders[name] for name in names}, len(folders) > limit


def _grams(text: str) -> set[str]:
    norm = _normalize(text)
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


# 图纸的题目在图签（右下角）：既有“平断面图3（1A11A~2A1）”这种带范围的图名，
# 也有“图名：XXX”这种字段行。正文开头全是图面注记，截前 800 字根本读不到题目。
_TITLE_FIELD = re.compile(r"^(?:图名|图纸名称)\s*[:：]\s*(\S.{0,39})$")
_DRAWING_LINE = re.compile(r"^[一-龥]{2,12}图\s*[\d一二三四五六七八九十]*\s*[（(][^）)\n]{1,24}[）)]$")


def drawing_title(text: str) -> str:
    """从整页文字里取图签上的图名，取不到返回空串。

    优先用改名模块的判据（认出出图专用章才算图纸，最稳）；退而看「图名：」字段，
    最后从页面尾部往上找带括号范围的图名行——图签总在页面最下方。
    """
    from .renamer import drawing_name

    name = drawing_name(text)
    if name:
        return name
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        found = _TITLE_FIELD.match(line)
        if found:
            return found.group(1).strip()
    for line in reversed(lines[-40:]):
        if _DRAWING_LINE.match(line):
            return line
    return ""


def excerpt_for(text: str, max_chars: int = 800) -> str:
    """给模型看的正文摘录：图纸把图签上的图名提到最前面。

    图纸正文前几百个字是塔号、角度这些图面注记，模型看不出这是哪张图；
    真正能回答「这张图是不是清单要的那张」的是图签上的图名。
    """
    text = (text or "").strip()
    if not text:
        return ""
    title = drawing_title(text)
    if title and title not in text[:120]:
        return f"图纸标题（取自图签）：{title}\n{text[:max_chars]}"
    return text[:max_chars]


def _duty_lines(cp: Any) -> list[str]:
    """【归档责任】段：立卷单位 + 清单来源。

    这两列是判断「该不该由施工单位归档」的唯一依据，也是人工判「口径差异」时用的口径；
    原来的写法把它们混在【清单要求】里，模型经常直接忽略。
    """
    filing = str(getattr(cp, "filing", "") or "").strip()
    source = str(getattr(cp, "source", "") or "").strip()
    if not filing and not source:
        return []
    lines = ["【归档责任】"]
    if filing:
        lines.append(f"立卷单位：{filing}")
    if source:
        lines.append(f"清单来源：{source}")
    return lines


def duty_sources(result: Any) -> list[str]:
    """【归档责任】里允许被引用的原文：字段原值 + prompt 里那两行的完整写法。"""
    cp = getattr(result, "checkpoint", None)
    if cp is None:
        return []
    filing = str(getattr(cp, "filing", "") or "").strip()
    source = str(getattr(cp, "source", "") or "").strip()
    out = []
    for label, value in (("立卷单位", filing), ("清单来源", source)):
        if value:
            out.extend([value, f"{label}：{value}"])
    return out


def build_prompt(
    result: Any,
    excerpt: str = "",
    nearby: Sequence[str] = (),
    gate: str = "",
    max_excerpt: int = 800,
    related: Sequence[str] = (),
    folders: Sequence[tuple[str, Sequence[str]]] = (),
    cases: Sequence[tuple[str, int]] = (),
    cases_truncated: bool = False,
) -> str:
    """拼一条复判请求。result 是 MatchResult，只有 checkpoint/evidence 会被用到。"""
    cp = result.checkpoint
    lines = [
        "【清单要求】",
        f"分类号：{cp.code}　类目：{cp.category}",
    ]
    if getattr(cp, "archive_content", ""):
        lines.append(f"归档内容：{cp.archive_content}")
    lines.append(f"主要项目文件：{cp.text}")
    lines.extend(_duty_lines(cp))
    if gate:
        lines.append(f"区分词（本组同名检查点靠它区分，命中才算本条的）：{gate}")

    evidence = list(getattr(result, "evidence", []) or [])
    if evidence:
        lines.append("【文件名候选】")
        for item in evidence[:3]:
            lines.append(f"- {Path(item.rel_path).name}（文件名匹配分 {item.score:.2f}）")
    else:
        lines.append("【文件名候选】未检索到候选文件")

    if nearby:
        lines.append("【同分类号目录里的其它文件（节选，用于判断并卷/异名）】")
        lines.extend(f"- {name}" for name in list(nearby)[:40])

    if related:
        lines.append("【全目录检索到的相关文件（可能在不同分类号目录，用于识别异名与并卷）】")
        lines.extend(f"- {name}" for name in list(related)[:15])

    if cases and not cases_truncated:
        lines.append(f"【全目录案卷清单（共 {len(cases)} 卷，括号里是该卷文件数）】")
        lines.extend(f"- {label}（{count}）" for label, count in cases)

    if folders:
        lines.append("【全目录里最相关的案卷（案卷名 + 其中文件，用于判断有没有并卷）】")
        for label, names in list(folders)[:8]:
            lines.append(f"- 案卷：{label}")
            lines.extend(f"  · {name}" for name in names)

    text = (excerpt or "").strip()
    if text:
        lines.append("【正文摘录】")
        lines.append(text[:max_excerpt])
    else:
        lines.append("【正文摘录】无（未读到正文）")

    lines.append("")
    lines.append(OUTPUT_SPEC)
    return "\n".join(lines)


def build_shortlist_prompt(
    result: Any, cases: Sequence[tuple[str, int]], gate: str = "", max_cases: int = 4
) -> str:
    """两段式的第一段：把案卷清单摊开，只问「哪几卷里可能有」。"""
    cp = result.checkpoint
    lines = [
        "【清单要求】",
        f"分类号：{cp.code}　类目：{cp.category}",
    ]
    if getattr(cp, "archive_content", ""):
        lines.append(f"归档内容：{cp.archive_content}")
    lines.append(f"主要项目文件：{cp.text}")
    lines.extend(_duty_lines(cp))
    if gate:
        lines.append(f"区分词：{gate}")
    lines.append(f"【全目录案卷清单（共 {len(cases)} 卷，括号里是该卷文件数）】")
    lines.extend(f"- {label}（{count}）" for label, count in cases)
    lines.append("")
    lines.append(_cases_spec(max_cases))
    return "\n".join(lines)


def nearby_names(code: str, files: Iterable[Any], limit: int = 40) -> list[str]:
    """同分类号目录下实际有的文件名，给大模型判断「并卷 / 异名」用。

    只取路径里带这个分类号前缀的文件；取不到就退而取同一二级目录。
    """
    code = str(code or "").strip()
    names: list[str] = []
    if not code:
        return names
    prefix = code[:4] if len(code) >= 4 else code
    for record in files:
        rel = str(getattr(record, "rel_path", ""))
        if prefix and prefix in rel:
            names.append(Path(rel).name)
        if len(names) >= limit:
            break
    return names


# --------------------------------------------------------------------------
# 解析（含 quote 校验与降级）
# --------------------------------------------------------------------------
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_KEEP = re.compile(r"[0-9A-Za-z一-鿿]")


def _normalize(text: str) -> str:
    return "".join(_KEEP.findall(text or "")).lower()


_LABEL = re.compile(r"^[^：:\n]{1,8}[：:]\s*(.+)$", re.DOTALL)


def _quote_supported(quote: str, sources: Sequence[str]) -> bool:
    norm_quote = _normalize(quote)
    if len(norm_quote) < 4:
        return False
    # 模型常把字段名一起抄进来（「立卷单位：建设 / 施工」），去掉标签再比一次
    found = _LABEL.match(str(quote or "").strip())
    stripped = _normalize(found.group(1)) if found else ""
    for source in sources:
        norm_source = _normalize(source)
        if norm_source and (norm_quote in norm_source or (stripped and stripped in norm_source)):
            return True
        # 模型偶尔会把长句截断或跨行拼接：取前 12 个字再试一次
        if len(norm_quote) > 12 and norm_quote[:12] in norm_source:
            return True
    return False


# 归档责任里出现这些词，才说明文件由对方单位出具/归档，才允许凭字段判「已提供」
_OTHER_UNITS = ("建设", "监理", "设计", "地方", "政府", "业主", "甲方", "物资", "运维")


def _duty_proves_other_unit(duty: Sequence[str]) -> bool:
    """立卷单位/来源里是否真的出现「施工」以外的单位。

    模型会拿「立卷单位：施工」当已提供的依据——那是自相矛盾：责任在施工单位，
    恰恰说明这份该由自己交，找不到就是缺。
    """
    return any(word in str(source) for source in duty for word in _OTHER_UNITS)


def parse_verdict(
    raw: str,
    excerpt: str = "",
    names: Sequence[str] = (),
    duty: Sequence[str] = (),
) -> LLMVerdict:
    """把模型返回的一段文本解析成 LLMVerdict。

    判「已提供」有两种依据，分别校验、互不替代：
    - basis=file：引卷内文件 / 正文摘录 / 文件名；
    - basis=duty：引清单【归档责任】里的立卷单位、来源原文。
    没写 basis 时两套源都认（兼容老模型），写了就必须落在对应那套源里。

    任何不确定都往「无法判断」降级——宁可不下结论，不给错结论。
    """
    text = (raw or "").strip()
    if not text:
        return LLMVerdict(verdict="unclear", error="模型返回为空")
    text = _FENCE.sub("", text).strip()
    match = _JSON_BLOCK.search(text)
    if not match:
        return LLMVerdict(verdict="unclear", error="返回里找不到 JSON")
    try:
        data = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return LLMVerdict(verdict="unclear", error="JSON 解析失败")
    if not isinstance(data, dict):
        return LLMVerdict(verdict="unclear", error="JSON 不是对象")

    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return LLMVerdict(verdict="unclear", error=f"verdict 取值不合法：{verdict!r}")

    quote = str(data.get("quote") or "").strip()
    reason = str(data.get("reason") or "").strip()[:80]
    basis = str(data.get("basis") or "").strip().lower()
    if basis == "duty":
        sources = [source for source in duty if source]
    elif basis == "file":
        sources = [excerpt, *names]
    else:  # 没声明依据：两套源都认
        sources = [excerpt, *names, *duty]
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0) or 0)))
    except (TypeError, ValueError):
        confidence = 0.0

    if verdict == "match" and not _quote_supported(quote, sources):
        # 「已提供」是在断言证据存在，必须能指到原文、文件名或清单字段，否则就是幻觉
        return LLMVerdict(
            verdict="unclear",
            confidence=0.0,
            reason="模型给的原文引用在材料里找不到，已降级",
            quote=quote,
            error="quote 校验未通过",
        )
    if verdict == "match" and not _quote_supported(quote, [excerpt, *names]):
        # 引用落在【归档责任】而不是文件上：那只有当责任确实写着别的单位时才算数
        if not _duty_proves_other_unit(duty):
            return LLMVerdict(
                verdict="unclear",
                confidence=0.0,
                reason="立卷单位/来源只写了施工单位，不能用它证明文件已提供",
                quote=quote,
                error="归档责任不支持已提供",
            )
    if verdict == "gap" and quote.strip() and not _quote_supported(quote, sources):
        # 「真缺」是在断言不存在，本来就没有可引用的原文；
        # 但如果它硬要引用一段，那段也必须真在材料里
        return LLMVerdict(
            verdict="unclear",
            confidence=0.0,
            reason="模型给的原文引用在材料里找不到，已降级",
            quote=quote,
            error="quote 校验未通过",
        )
    return LLMVerdict(verdict=verdict, confidence=confidence, reason=reason, quote=quote)


def _match_case_name(name: str, labels: Sequence[str]) -> str:
    """把模型写的案卷名对齐到真实案卷名：先精确，再双向包含。

    模型常会把编号后面的说明略写（「AX-8230-10 设计变更文件」），
    所以子串命中即可；反过来模型啰嗦一截时，真实案卷名是它的子串也算。
    """
    name = str(name or "").strip()
    if not name:
        return ""
    norm = _normalize(name)
    if not norm:
        return ""
    if name in labels:
        return name
    for label in labels:
        if _normalize(label) == norm:
            return label
    for label in labels:
        norm_label = _normalize(label)
        if norm_label and (norm_label in norm or norm in norm_label):
            return label
    return ""


def parse_shortlist(raw: str, labels: Sequence[str], limit: int = 3) -> list[str]:
    """解析第一段的「哪几卷」，返回真实案卷名（对不上的一律丢掉）。"""
    text = (raw or "").strip()
    if not text:
        return []
    text = _FENCE.sub("", text).strip()
    match = _JSON_BLOCK.search(text)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict):
        return []
    picked: list[str] = []
    for item in data.get("cases") or []:
        label = _match_case_name(str(item), labels)
        if label and label not in picked:
            picked.append(label)
        if len(picked) >= limit:
            break
    return picked


@dataclass
class JudgeRequest:
    """一条复判请求的全部输入。两段式编排只在 judge_request 里做一次。"""
    result: Any
    excerpt: str = ""
    nearby: Sequence[str] = ()
    related: Sequence[str] = ()
    gate: str = ""
    cases: Sequence[tuple[str, int]] = ()
    cases_truncated: bool = False
    case_map: dict[str, list[str]] = None  # type: ignore[assignment]
    fallback_folders: Sequence[tuple[str, Sequence[str]]] = ()

    def __post_init__(self) -> None:
        if self.case_map is None:
            self.case_map = {}


def judge_request(judge: "LLMJudge", request: JudgeRequest, max_cases: int = 4) -> LLMVerdict:
    """两段式复判：① 模型从案卷清单里挑卷 → ② 把那几卷的文件名摊开，再下结论。

    检索交给模型（它懂案卷编号与专业的关系），取证交给代码（只给真实存在的文件名）。
    第一段失败或没挑出卷时，退回按词面检索到的案卷，不让整条复判失败。
    """
    picked: list[str] = []
    if request.cases and not request.cases_truncated:
        answer = judge.ask(
            build_shortlist_prompt(request.result, request.cases, request.gate, max_cases),
            SHORTLIST_SYSTEM,
        )
        if not answer.error:
            picked = parse_shortlist(answer.raw, [label for label, _ in request.cases],
                                     limit=max_cases)

    folders: list[tuple[str, Sequence[str]]] = []
    for label in picked:
        # 一卷平均不到 10 份，25 个上限基本等于整卷摊开；挑 4 卷时上下文也就 100 行
        folders.append((label, request.case_map.get(label, [])[:25]))
    if not folders:
        folders = list(request.fallback_folders)

    prompt = build_prompt(
        request.result, request.excerpt, nearby=request.nearby, gate=request.gate,
        related=request.related, folders=folders,
    )
    names = ([Path(ev.rel_path).name for ev in getattr(request.result, "evidence", []) or []]
             + list(request.nearby) + list(request.related)
             + [label for label, _ in folders]
             + [name for _, names_ in folders for name in names_])
    verdict = judge.judge(prompt, request.excerpt, names, duty_sources(request.result))
    if picked:
        prefix = "先指认案卷 " + "、".join(picked)
        verdict.reason = f"{prefix}；{verdict.reason}" if verdict.reason else prefix
        verdict.reason = verdict.reason[:120]
    return verdict


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class LLMJudge:
    """一次复判任务的客户端：缓存、重试、并发都在这里。"""

    def __init__(
        self,
        config: LLMConfig,
        cache_path: str | Path | None = None,
        session: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = config
        self.cache_path = Path(cache_path) if cache_path else None
        self.session = session
        self._sleep = sleep
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self.calls = 0
        self.hits = 0
        self.errors = 0
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8")).get("items", {})
            except Exception:  # noqa: BLE001
                self._cache = {}

    # -- 缓存 ---------------------------------------------------------
    def _key(self, prompt: str, system: str = "") -> str:
        raw = f"{self.config.style}|{self.config.base_url}|{self.config.model}|{system}|{prompt}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def save(self) -> None:
        if not self.cache_path or not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "items": self._cache}
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._dirty = False

    # -- HTTP ---------------------------------------------------------
    def _post(self, prompt: str, system: str = SYSTEM_PROMPT) -> str:
        if requests is None:
            raise RuntimeError("缺少 requests，无法联网复判")
        cfg = self.config
        if cfg.style == "anthropic":
            url = f"{cfg.base_url.rstrip('/')}/messages"
            headers = {
                "x-api-key": cfg.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": cfg.model,
                "max_tokens": 400,
                "temperature": cfg.temperature,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        else:
            url = f"{cfg.base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {cfg.api_key}",
                "content-type": "application/json",
            }
            body = {
                "model": cfg.model,
                "temperature": cfg.temperature,
                "max_tokens": 400,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
        client = self.session or requests
        response = client.post(url, headers=headers, json=body, timeout=cfg.timeout)
        status = getattr(response, "status_code", 200)
        if status >= 400:
            raise _HTTPError(status, _safe_text(response))
        data = response.json()
        if cfg.style == "anthropic":
            blocks = data.get("content") or []
            return "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("返回里没有 choices")
        return str((choices[0].get("message") or {}).get("content", ""))

    def _post_with_retry(self, prompt: str, system: str = SYSTEM_PROMPT) -> str:
        cfg = self.config
        last: Exception | None = None
        for attempt in range(max(1, cfg.retries)):
            try:
                return self._post(prompt, system)
            except _HTTPError as exc:
                last = exc
                if exc.status in (400, 401, 403, 404):
                    raise  # 配置类错误，重试没有意义
            except Exception as exc:  # noqa: BLE001 - 网络/解析，可重试
                last = exc
            if attempt < cfg.retries - 1:
                self._sleep(min(8.0, 1.5 ** attempt))
        raise last if last else RuntimeError("未知错误")

    # -- 对外接口 -----------------------------------------------------
    def ask(self, prompt: str, system: str = SYSTEM_PROMPT) -> "LLMAnswer":
        """发一条请求，返回模型原文。命中缓存时不发网络、费用为 0。"""
        key = self._key(prompt, system)
        with self._lock:
            cached = self._cache.get(key)
        if isinstance(cached, dict) and "raw" in cached:
            self.hits += 1
            return LLMAnswer(raw=str(cached["raw"]), cached=True)
        try:
            raw = self._post_with_retry(prompt, system)
        except _HTTPError as exc:
            self.errors += 1
            return LLMAnswer(error=f"HTTP {exc.status}：{exc.text[:80]}")
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            return LLMAnswer(error=f"{type(exc).__name__}：{str(exc)[:80]}")

        with self._lock:
            self.calls += 1
            self._cache[key] = {"raw": raw}
            self._dirty = True
        return LLMAnswer(raw=raw)

    def judge(
        self,
        prompt: str,
        excerpt: str = "",
        names: Sequence[str] = (),
        duty: Sequence[str] = (),
        system: str = SYSTEM_PROMPT,
    ) -> LLMVerdict:
        answer = self.ask(prompt, system)
        if answer.error:
            return LLMVerdict(verdict="unclear", error=answer.error)
        verdict = parse_verdict(answer.raw, excerpt, names, duty)
        verdict.cached = answer.cached
        if not verdict.ok:
            with self._lock:
                self.errors += 1
        return verdict

    def judge_many(self, items: Sequence[tuple[str, str, str, Sequence[str]]]) -> dict[str, LLMVerdict]:
        """items = [(id, prompt, excerpt, names)]，并发跑，返回 id → 判定。"""
        def run(item: tuple[str, str, str, Sequence[str]]) -> tuple[str, LLMVerdict]:
            key, prompt, excerpt, names = item
            return key, self.judge(prompt, excerpt, names)

        if not items:
            return {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as pool:
            return dict(pool.map(run, items))

    def judge_requests(self, jobs: Sequence[tuple[str, "JudgeRequest"]]) -> dict[str, LLMVerdict]:
        """jobs = [(id, JudgeRequest)]，两段式复判并发跑。"""
        def run(job: tuple[str, "JudgeRequest"]) -> tuple[str, LLMVerdict]:
            key, request = job
            return key, judge_request(self, request)

        if not jobs:
            return {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as pool:
            return dict(pool.map(run, jobs))


class _HTTPError(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"HTTP {status}: {text[:120]}")
        self.status = status
        self.text = text


def _safe_text(response: Any) -> str:
    try:
        return str(response.text)
    except Exception:  # noqa: BLE001
        return ""


def override_state(result: Any) -> str | None:
    """LLM 建议 → 人工采纳后写进 OverrideStore 的状态。无法判断的不改判。"""
    from .models import STATE_MATCHED, STATE_MISSING

    return {"match": STATE_MATCHED, "gap": STATE_MISSING}.get(getattr(result, "llm_verdict", ""))


def override_entry(result: Any, reviewer: str = "") -> dict[str, Any] | None:
    """构造一条人工纠正记录；采纳后仍可撤销、有日志。"""
    state = override_state(result)
    if state is None:
        return None
    from datetime import datetime

    note = f"采纳大模型建议：{getattr(result, 'llm_reason', '')}"
    if getattr(result, "llm_quote", ""):
        note += f"（{result.llm_quote}）"
    return {
        "state": state,
        "note": note,
        "reviewer": reviewer,
        "time": datetime.now().isoformat(timespec="seconds"),
    }


def apply_verdict(result: Any, verdict: LLMVerdict) -> None:
    """把判定写回 MatchResult 的 llm_* 字段（不动 state —— 改判只走人工通道）。"""
    result.llm_verdict = verdict.verdict
    result.llm_reason = verdict.reason
    result.llm_quote = verdict.quote
    result.llm_confidence = verdict.confidence
    result.llm_cached = verdict.cached
    result.llm_error = verdict.error


def estimate_tokens(prompts: Sequence[str]) -> int:
    """粗略估算输入 token：中文约 1.6 字符/token。只用于点按钮前给个量级。"""
    return int(sum(len(p) for p in prompts) / 1.6)
