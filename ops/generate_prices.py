#!/usr/bin/env python3
"""Publish the live customer group token prices to the static public price page."""

from decimal import Decimal
from datetime import datetime
from html import escape
from pathlib import Path
import re
from zoneinfo import ZoneInfo
import json

from ops import admin_api as api
from ops import admin_api as unified


OUTPUT = Path(__file__).resolve().parents[1] / "frontend/public/model-prices.html"
# The customer group was created as 'all' and may be renamed in the admin UI.
CUSTOMER_GROUP_ID = 9
MANIFEST = Path(__file__).resolve().parents[1] / "resources/model-pricing/official-site-prices.json"
TOOL_IDS = frozenset({"image_generation", "image_recognition", "web_search"})
FAMILY = {
    "gpt-image-": "图片生成",
    "gpt-6": "Codex", "gpt-5.6": "Codex",
    "claude-": "Claude", "gemini-": "Gemini", "deepseek-": "DeepSeek",
    "glm-": "GLM", "kimi-": "Kimi", "minimax-": "MiniMax", "qwen": "Qwen",
    "gte-": "GTE",
    "nemotron-": "NVIDIA", "step-": "阶跃星辰",
    "image_generation": "工具", "image_recognition": "工具", "web_search": "工具",
}
PRICE_COLUMNS = (
    "input_price", "output_price", "cache_read_price", "cache_write_price",
    "cache_write_1h_price", "image_output_price",
)


def per_million(value):
    if value is None:
        return "—"
    amount = (Decimal(str(value)) * Decimal(1_000_000)).quantize(Decimal("0.000001"))
    return "$" + format(amount.normalize(), "f")


def family(model):
    return next(label for prefix, label in FAMILY.items() if model.lower().startswith(prefix))


def main():
    token = unified.login()
    group = api.request(f"/api/v1/admin/groups/{CUSTOMER_GROUP_ID}", token)
    if group["platform"] != "composite" or group["status"] != "active":
        raise RuntimeError("Public price page requires the active customer composite group")
    allowed = set(group["model_allowlist"]["models"])
    prices = {model: entry for entry in group["model_pricing"] for model in entry["models"]}
    catalog = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    delegated = catalog.get("delegated_capabilities", {})
    if allowed != set(prices) | set(delegated):
        raise RuntimeError("Public price page cannot be generated from incomplete pricing")
    if delegated:
        plaza = api.request("/api/v1/model-plaza")
        public = next(item for item in plaza["groups"] if item["id"] == CUSTOMER_GROUP_ID)
        live_prices = {item["name"]: item["pricing"] for item in public["models"]}
        for model in delegated:
            if not live_prices.get(model):
                raise RuntimeError("Delegated tool has no live public price: " + model)
            prices[model] = live_prices[model]
    rows = []
    sources = {**catalog.get("models", {}), **delegated}
    source_labels = {
        "developer_standard": "原厂官方价", "upstream_official": "七牛官方价",
        "hosting_reference": "本地·托管参考", "image_usage_pending": "本站按张价",
        "no_developer_token_tariff": "本站价·无官方 Token 价",
        "reseller_alias": "转接商别名",
        "site_standard": "本站售价", "site_image": "本站按张价",
    }
    for model in sorted(allowed, key=lambda name: (list(FAMILY.values()).index(family(name)), name)):
        entry = prices[model]
        if model == "image_generation":
            cells = '<td>—</td>' * 5 + '<td>按所选图片模型及分辨率</td>'
        elif entry.get("billing_mode") == "per_request":
            cells = '<td>—</td>' * 5 + f'<td>${entry["per_request_price"]:g} / 次</td>'
        elif model in unified.IMAGE_MODELS or entry.get("billing_mode") == "image":
            tiers = entry.get("intervals") or []
            image_price = "<br>".join(f'{escape(tier["tier_label"])} ${tier["per_request_price"]:g} / 张' for tier in tiers)
            cells = '<td>—</td>' * 5 + f'<td>{image_price or "$"+format(entry["per_request_price"], "g")+" / 张"}</td>'
        else:
            cells = "".join(f"<td>{per_million(entry.get(column))}</td>" for column in PRICE_COLUMNS)
        label = family(model)
        source = sources.get(model)
        origin = (f'<a href="{escape(source["source"], quote=True)}" target="_blank" rel="noopener noreferrer" '
                  f'title="{escape(source["note"], quote=True)}">{escape(source_labels[source["status"]])}</a>'
                  if source else "待核对")
        rows.append(
            f'<tr data-search="{escape((label + " " + model).lower())}">'
            f'<td><span class="docs-family">{escape(label)}</span><code>{escape(model)}</code></td>'
            f'{cells}<td>{origin}</td></tr>'
        )
    content = OUTPUT.read_text()
    start, end = "<!-- PRICE_ROWS_START -->", "<!-- PRICE_ROWS_END -->"
    if content.count(start) != 1 or content.count(end) != 1:
        raise RuntimeError("Price table markers missing or duplicated")
    before, remainder = content.split(start, 1)
    _, after = remainder.split(end, 1)
    content = before + start + "\n" + "\n".join(rows) + "\n" + end + after
    content = re.sub(r'(<span\b[^>]*\bid="count"[^>]*>)[^<]*(</span>)',
                     rf'\g<1>{len(allowed - TOOL_IDS)} 个模型 · {len(allowed & TOOL_IDS)} 个工具\g<2>', content, count=1)
    content = re.sub(r'<div class="docs-sources">.*?</div>', '', content, flags=re.S)
    content = re.sub(r'<p class="docs-note">.*?</p>', '', content, flags=re.S)
    content = re.sub(r'更新于 \d{4}-\d{2}-\d{2}', '更新于 '+datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(), content)
    content = re.sub(r'<p class="docs-fast-note">.*?</p>', '', content, flags=re.S)
    multipliers = sorted({entry["fast_multiplier"] for entry in prices.values() if entry.get("fast_multiplier")})
    if multipliers:
        multiple = " / ".join(format(value, "g") for value in multipliers)
        content = content.replace('<div class="docs-tools">', f'<p class="docs-fast-note">Codex Fast 为标准 Token 单价的 {multiple} 倍，包含输入、输出与缓存计费。API 传入 <code>service_tier: "fast"</code> 即可开启。输入超过 272,000 Token 时，整次请求输入及缓存 ×2、输出 ×1.5。</p><div class="docs-tools">', 1)
    content = re.sub(r'<p class="docs-lead">.*?</p>', '<p class="docs-lead">以下为当前实际扣费单价。文本、向量与重排按美元 / 百万 Token 计费；图片按模型、分辨率和实际生成张数计费。来源列标明官方价或本站售价。</p>', content, count=1, flags=re.S)
    content = content.replace('<th>图片输出</th></tr>', '<th>图片输出</th><th>计价来源</th></tr>')
    content = content.replace('<th>图片输出</th>', '<th>图片 / 工具计费</th>')
    notes = ('<div class="docs-sources"><p>Qwen 4B、Qwen Flash/Max 和 Nemotron 使用本站售价，包含推理、上游与运维服务费用。'
             '来源链接用于查看官方或上游参考资料，不表示本站售价是原厂官方价。'
             'DeepSeek 采用七牛繁忙时段固定参考价，未实现原厂峰谷自动切换。</p>'
             '<p>图片使用上表各模型独立的 1K / 2K / 4K 每张售价，按实际生成张数计费；任务轮询不重复收取图片费用。'
             '这些是本站按张售价，不是原厂按 Token 计算的价格。</p>'
             '<p>本地 Embedding 向量和 Reranker 排序结果不另收输出费。'
             '工具入口按上表计费，生图选型入口沿用所选模型价格；识图按张、联网搜索按次。'
             '“—”表示没有独立配置该项单价，不代表官方承诺免费。</p></div>')
    content = content.replace('<div class="docs-foot">', notes+'<div class="docs-foot">', 1)
    OUTPUT.write_text(content)
    print(f"Published {len(rows)} models to {OUTPUT}")


if __name__ == "__main__":
    main()
