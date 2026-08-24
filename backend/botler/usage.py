"""任务 token 用量采集与费用估算（issue #235）。

三个执行引擎的用量数据来源（原始数据均现成，此前未采集）：
- claude：stream-json / --output-format json 输出尾部 result 事件行的
  usage 字段（input_tokens / cache_creation_input_tokens /
  cache_read_input_tokens / output_tokens）与 total_cost_usd（SDK 自带
  费用，含缓存计价，优先采用）；
- dsh：deepseek-harness SDK 通知流里 assistant/chunk 事件中
  type=usage 的 chunk（usage.prompt_tokens / completion_tokens /
  total_tokens，DeepSeek OpenAI 兼容字段）；
- hermes：run_conversation 后 agent 的会话级计数器
  （session_prompt_tokens / session_completion_tokens /
  session_total_tokens / session_estimated_cost_usd）。

费用估算：优先引擎自带费用（claude total_cost_usd / hermes
session_estimated_cost_usd）；否则按 config usage.pricing 单价表
（每百万 token 单价，model 支持子串匹配）估算；无单价 → 返回 None，
前端只展示 token 数。全部函数尽力而为：解析失败返回 None，不抛异常、
不阻塞任务收尾。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_cache_hit_rate(hit_tokens, miss_tokens) -> float | None:
    """计算 DeepSeek 提示词缓存命中率（issue #473）。

    输入为 usage chunk 的 prompt_cache_hit_tokens / prompt_cache_miss_tokens
    （DeepSeek OpenAI 兼容字段），返回命中率百分比（0~100，保留 1 位小数）。
    两者都无/为 0（缓存未启用或非 dsh 引擎）→ None（前端不展示缓存行）。
    数值可能以字符串到达（SDK 透传），统一转 int 防御（转不了视为无数据）。
    """
    try:
        hit = None if hit_tokens is None else int(hit_tokens)
        miss = None if miss_tokens is None else int(miss_tokens)
    except (TypeError, ValueError):
        return None
    if hit is None or miss is None:
        return None
    total = hit + miss
    if total <= 0:
        return None
    return round(hit / total * 100, 1)


def normalize_model_name(model) -> str:
    """模型名归一化（小写、去空白），用于单价表匹配。"""
    if not isinstance(model, str):
        return ""
    return model.strip().lower()


def find_pricing(pricing, model) -> dict | None:
    """在单价表中匹配模型：先精确匹配，再子串匹配（取首个命中）。

    pricing 每项为 {"model", "input_per_million", "output_per_million"}；
    model 缺失/为空、pricing 非列表都返回 None。精确匹配优先保证
    「deepseek-v4-flash」不会被更宽的「deepseek」先命中。
    """
    if not pricing or not isinstance(pricing, list):
        return None
    name = normalize_model_name(model)
    if not name:
        return None
    entries = [p for p in pricing if isinstance(p, dict)
               and normalize_model_name(p.get("model"))]
    for p in entries:
        if normalize_model_name(p.get("model")) == name:
            return p
    for p in entries:
        key = normalize_model_name(p.get("model"))
        if key and key in name:
            return p
    return None


def estimate_cost(model, prompt_tokens, completion_tokens,
                  pricing, currency: str = "USD") -> tuple[float, str] | None:
    """按单价表估算费用（每百万 token 单价）。

    单价项缺 input_per_million / output_per_million（非数字）时按 0 处理；
    单价表无匹配项返回 None（无单价只展示 token 数）。返回 (费用, 货币)。
    """
    entry = find_pricing(pricing, model)
    if entry is None:
        return None
    in_price = _to_float(entry.get("input_per_million")) or 0.0
    out_price = _to_float(entry.get("output_per_million")) or 0.0
    if in_price <= 0 and out_price <= 0:
        return None
    cost = (prompt_tokens / 1_000_000 * in_price
            + completion_tokens / 1_000_000 * out_price)
    return cost, str(entry.get("currency") or currency or "USD")


# ---- claude 引擎：result 事件行 usage ----

def parse_claude_result_usage(result_data) -> dict | None:
    """解析 claude 结果事件（type=result）→ 用量 dict。

    输入为 stream-json 尾部 result 事件行解析出的 dict（或
    --output-format json 的单行 result），要求带 usage 字段：
      usage.input_tokens / cache_creation_input_tokens /
      cache_read_input_tokens / output_tokens（Claude Code 2.x 标准字段）
    输出：
      {prompt_tokens（input + 缓存读写）, completion_tokens, total_tokens,
       model（modelUsage 首个 canonicalModel，缺失回退 None）,
       sdk_cost（total_cost_usd，SDK 自带费用）,
       raw_usage（usage 原样）}
    非 result / 缺 usage → None（旧任务或异常中断无用量数据）。
    """
    if not isinstance(result_data, dict):
        return None
    if result_data.get("type") not in (None, "result"):
        return None
    usage = result_data.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _to_int(usage.get("input_tokens"))
    cache_read = _to_int(usage.get("cache_read_input_tokens"))
    cache_creation = _to_int(usage.get("cache_creation_input_tokens"))
    prompt = input_tokens + cache_read + cache_creation
    completion = _to_int(usage.get("output_tokens"))
    model = None
    model_usage = result_data.get("modelUsage")
    if isinstance(model_usage, dict):
        for m in model_usage.values():
            if isinstance(m, dict) and m.get("canonicalModel"):
                model = m["canonicalModel"]
                break
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "model": model,
        "sdk_cost": _to_float(result_data.get("total_cost_usd")),
        "raw_usage": usage,
    }


# ---- dsh 引擎：SDK 通知流 usage chunk ----

def extract_dsh_usage(events) -> dict | None:
    """从 dsh SDK 会话事件列表聚合 token 用量（assistant/chunk 的 usage chunk）。

    deepseek-harness runtime 在每次模型调用结束时会发
    session.event → event.data.chunk.type == "usage"（chunk.usage 为
    DeepSeek OpenAI 兼容字段：prompt_tokens / completion_tokens /
    total_tokens / prompt_cache_hit_tokens / prompt_cache_miss_tokens，
    其中 prompt_tokens = 缓存命中 + 未命中，issue #473 用于缓存命中率）。
    一个会话可能有多次模型调用（多回合/工具循环），这里逐事件累加
    （total 缺失时按 prompt + completion 兜底）。
    事件列表为空 / 无 usage chunk → None。
    """
    if not isinstance(events, list):
        return None
    prompt = completion = total = 0
    cache_hit = cache_miss = 0
    found = False
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant/chunk":
            continue
        data = event.get("data")
        chunk = data.get("chunk") if isinstance(data, dict) else None
        if not isinstance(chunk, dict) or chunk.get("type") != "usage":
            continue
        usage = chunk.get("usage")
        if not isinstance(usage, dict):
            continue
        p = _to_int(usage.get("prompt_tokens"))
        c = _to_int(usage.get("completion_tokens"))
        t = _to_int(usage.get("total_tokens")) or (p + c)
        prompt += p
        completion += c
        total += t
        cache_hit += _to_int(usage.get("prompt_cache_hit_tokens"))
        cache_miss += _to_int(usage.get("prompt_cache_miss_tokens"))
        found = True
    if not found:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or (prompt + completion),
        "prompt_cache_hit_tokens": cache_hit,
        "prompt_cache_miss_tokens": cache_miss,
        "model": None,
        "sdk_cost": None,
        "raw_usage": {"prompt_tokens": prompt,
                      "completion_tokens": completion,
                      "total_tokens": total or (prompt + completion),
                      "prompt_cache_hit_tokens": cache_hit,
                      "prompt_cache_miss_tokens": cache_miss},
    }


# ---- 费用估算统一入口 ----

def finalize_usage(engine: str, usage: dict | None, *,
                   model: str | None = None,
                   pricing=None, currency: str = "USD") -> dict | None:
    """把引擎采集的用量 dict 归一化为落库记录（含费用估算）。

    usage 为 None（引擎无用量数据）→ 返回 None（不落库，前端显示无数据）。
    费用优先级：引擎自带费用（sdk_cost）> config 单价估算 > None。
    """
    if not usage:
        return None
    prompt = _to_int(usage.get("prompt_tokens"))
    completion = _to_int(usage.get("completion_tokens"))
    total = _to_int(usage.get("total_tokens")) or (prompt + completion)
    model = model or usage.get("model")
    cost = _to_float(usage.get("sdk_cost"))
    final_currency = currency or "USD"
    if cost is None:
        estimated = estimate_cost(model, prompt, completion, pricing, final_currency)
        if estimated is not None:
            cost, final_currency = estimated
    return {
        "model": model if isinstance(model, str) and model else None,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated_cost": round(cost, 6) if cost is not None else None,
        "currency": final_currency,
        "raw_usage": usage.get("raw_usage"),
    }
