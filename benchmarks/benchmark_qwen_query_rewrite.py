#!/usr/bin/env python3
"""
比较多个千问模型执行 Nalook Query Rewrite 的速度和基础输出质量。

特点：
- 仅使用 Python 标准库；
- 走阿里云百炼 OpenAI 兼容 Chat Completions 接口；
- 流式请求，同时记录首个内容 Token 延迟（TTFT）和总耗时；
- 默认测试 4 个模型、12 条不同复杂度 Query；
- 输出逐条 CSV 和汇总 JSON。

使用方式：
1. 设置环境变量 DASHSCOPE_API_KEY。
2. 运行：
   python3 搜索系统/benchmark_qwen_query_rewrite.py

常用选项：
   --repeats 3                 每个 Query/模型重复 3 次
   --models qwen-flash qwen-turbo
   --no-warmup                不执行预热请求
   --timeout 30               单次请求超时秒数
   --output-dir ./results     指定结果目录
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


# API Key 仅从环境变量读取。
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODELS = [
    "qwen-flash",
    "qwen-turbo",
    "qwen3.6-flash",
    "qwen3.7-flash",
]

# 与当前后端 PostKeywordExpansionProcessController 的 Prompt 保持一致，使对比更接近生产。
REWRITE_PROMPT = (
    "你是一个搜索 Query 改写助手。用户会给你一段搜索关键词。请输出严格的 JSON 对象，字段仅两个："
    '"query": 对用户输入的规范化改写（保持原始检索意图，可修正明显错别字/口语化表达，'
    "但不得改变含义、不得凭空新增原文没有的硬性限定条件）；"
    '"expansion_queries": 一个长度恰好为 2 的字符串数组，每条都是与 query 语义等价的同义改写/换一种说法，'
    "用于扩大召回，同样不得凭空新增原文没有的硬性限定条件（如时间、地点、品牌、数量等原文未出现的约束）。"
    "扩展词必须是原查询的同义/近义表达；不得给专有名词、人名添加称谓或角色词（如 先生/女士/小姐/博士/老师/教授/同学 等），"
    "也不得添加原文没有的通用后缀或修饰词。"
    "不要输出 intentions 或任何其它字段。"
    "只输出 JSON 对象本身，不要额外解释，不要用 markdown 代码块包裹，不含任何 markdown 符号。"
)


@dataclass(frozen=True)
class QueryCase:
    query_id: str
    complexity: str
    query: str
    challenge: str


QUERY_CASES = [
    # 简单：短词、人名、型号，重点看是否过度扩展。
    QueryCase("S01", "simple", "露营", "单一主题短词"),
    QueryCase("S02", "simple", "张三", "人名，不应添加老师/先生等称谓"),
    QueryCase("S03", "simple", "iPhone 17 Pro", "英文品牌和型号，不应改变型号"),
    QueryCase("S04", "simple", "杭州亲子露营地", "明确地点和人群约束"),

    # 中等：错别字、口语、否定条件和故障问题。
    QueryCase("M01", "medium", "杭洲有什么适合大人带小孩的露营地", "错别字纠正且保留地点/亲子约束"),
    QueryCase("M02", "medium", "预算三千以内适合新手的微单相机", "价格、人群和品类多约束"),
    QueryCase("M03", "medium", "想找不辣但有杭州特色的餐厅", "否定条件不能在改写中丢失"),
    QueryCase("M04", "medium", "MacBook 连不上公司 Wi-Fi 怎么办", "中英文混写和故障排查意图"),

    # 复杂：长句、多个硬约束、比较和模糊自然语言。
    QueryCase(
        "C01",
        "complex",
        "下周末从上海出发，带五岁小孩玩两天，不自驾，想找高铁两小时内能到的地方",
        "时间、出发地、儿童、交通和时长约束",
    ),
    QueryCase(
        "C02",
        "complex",
        "我想买一台主要拍宠物和室内视频的相机，预算不超过八千，不要佳能，最好防抖好一点",
        "用途、预算、排除品牌和性能偏好",
    ),
    QueryCase(
        "C03",
        "complex",
        "Docker Desktop 在 M3 Mac 上运行 amd64 镜像特别慢，怎么优化但不想换电脑",
        "技术术语、芯片平台、性能问题和否定约束",
    ),
    QueryCase(
        "C04",
        "complex",
        "最近工作压力很大，晚上总是睡不着，想看看有没有不吃药也能改善睡眠的方法",
        "口语化健康问题，保留不用药的否定约束",
    ),
]


@dataclass
class RunResult:
    model: str
    query_id: str
    complexity: str
    query: str
    challenge: str
    repeat: int
    success: bool
    http_status: int | None
    ttft_ms: float | None
    total_ms: float | None
    output_chars: int
    json_valid: bool
    canonical_nonempty: bool
    expansion_count: int | None
    exactly_two_expansions: bool
    canonical_query: str
    expansion_queries: str
    raw_output: str
    error: str


def normalize_query(raw: str) -> str:
    """对齐生产代码：trim -> 折叠空白 -> NFKC -> 小写 -> 200 字。"""
    collapsed = " ".join(raw.strip().split())
    return unicodedata.normalize("NFKC", collapsed).lower()[:200]


def extract_json(text: str) -> str:
    """对齐生产 JsonExtractUtils：去围栏，截取首个 { 到最后一个 }。"""
    value = text.strip()
    if value.startswith("```"):
        if value.startswith("```json"):
            value = value[len("```json") :]
        else:
            value = value[len("```") :]
        if value.endswith("```"):
            value = value[: -len("```")]
        value = value.strip()
    start = value.find("{")
    end = value.rfind("}")
    return value[start : end + 1] if start >= 0 and end > start else value


def inspect_output(text: str) -> tuple[bool, bool, int | None, bool, str, str]:
    try:
        obj = json.loads(extract_json(text))
    except (json.JSONDecodeError, TypeError):
        return False, False, None, False, "", ""

    if not isinstance(obj, dict):
        return False, False, None, False, "", ""

    canonical = obj.get("query")
    canonical_nonempty = isinstance(canonical, str) and bool(canonical.strip())
    expansions = obj.get("expansion_queries")
    expansion_count = len(expansions) if isinstance(expansions, list) else None
    expansions_are_strings = isinstance(expansions, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in expansions
    )
    exactly_two = expansion_count == 2 and expansions_are_strings
    expansion_text = json.dumps(expansions, ensure_ascii=False) if isinstance(expansions, list) else ""
    return True, canonical_nonempty, expansion_count, exactly_two, canonical.strip() if canonical_nonempty else "", expansion_text


def error_result(model: str, case: QueryCase, repeat: int, started: float, error: str, status: int | None = None) -> RunResult:
    return RunResult(
        model=model,
        query_id=case.query_id,
        complexity=case.complexity,
        query=case.query,
        challenge=case.challenge,
        repeat=repeat,
        success=False,
        http_status=status,
        ttft_ms=None,
        total_ms=round((time.perf_counter() - started) * 1000, 2),
        output_chars=0,
        json_valid=False,
        canonical_nonempty=False,
        expansion_count=None,
        exactly_two_expansions=False,
        canonical_query="",
        expansion_queries="",
        raw_output="",
        error=error,
    )


def run_streaming_request(
    *,
    api_key: str,
    base_url: str,
    model: str,
    case: QueryCase,
    repeat: int,
    timeout: float,
    max_tokens: int,
) -> RunResult:
    normalized = normalize_query(case.query)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content": normalized},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": True,
        "enable_thinking": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )

    started = time.perf_counter()
    first_content_at: float | None = None
    content_parts: list[str] = []
    status: int | None = None

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    if first_content_at is None:
                        first_content_at = time.perf_counter()
                    content_parts.append(piece)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        return error_result(model, case, repeat, started, f"HTTPError: {detail}", exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return error_result(model, case, repeat, started, f"{type(exc).__name__}: {exc}", status)
    except Exception as exc:  # 让单个模型失败不中断整个 benchmark。
        return error_result(model, case, repeat, started, f"{type(exc).__name__}: {exc}", status)

    finished = time.perf_counter()
    output = "".join(content_parts).strip()
    json_valid, canonical_nonempty, expansion_count, exactly_two, canonical, expansions = inspect_output(output)
    success = status == 200 and bool(output)

    return RunResult(
        model=model,
        query_id=case.query_id,
        complexity=case.complexity,
        query=case.query,
        challenge=case.challenge,
        repeat=repeat,
        success=success,
        http_status=status,
        ttft_ms=round((first_content_at - started) * 1000, 2) if first_content_at else None,
        total_ms=round((finished - started) * 1000, 2),
        output_chars=len(output),
        json_valid=json_valid,
        canonical_nonempty=canonical_nonempty,
        expansion_count=expansion_count,
        exactly_two_expansions=exactly_two,
        canonical_query=canonical,
        expansion_queries=expansions,
        raw_output=output,
        error="" if success else "empty streaming response",
    )


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(probability * len(ordered)) - 1)
    return round(ordered[index], 2)


def metric_summary(rows: list[RunResult]) -> dict[str, Any]:
    successes = [row for row in rows if row.success]
    ttfts = [row.ttft_ms for row in successes if row.ttft_ms is not None]
    totals = [row.total_ms for row in successes if row.total_ms is not None]

    def mean(values: list[float]) -> float | None:
        return round(statistics.fmean(values), 2) if values else None

    def median(values: list[float]) -> float | None:
        return round(statistics.median(values), 2) if values else None

    return {
        "runs": len(rows),
        "successful_runs": len(successes),
        "success_rate": round(len(successes) / len(rows), 4) if rows else 0.0,
        "ttft_ms": {
            "mean": mean(ttfts),
            "median": median(ttfts),
            "p95": percentile(ttfts, 0.95),
        },
        "total_ms": {
            "mean": mean(totals),
            "median": median(totals),
            "p95": percentile(totals, 0.95),
        },
        "json_valid_rate": round(sum(row.json_valid for row in successes) / len(successes), 4) if successes else 0.0,
        "exactly_two_expansions_rate": round(
            sum(row.exactly_two_expansions for row in successes) / len(successes), 4
        )
        if successes
        else 0.0,
        "average_output_chars": round(statistics.fmean(row.output_chars for row in successes), 2) if successes else None,
    }


def build_summary(results: list[RunResult], models: list[str], repeats: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "models": models,
        "query_count": len(QUERY_CASES),
        "repeats": repeats,
        "query_cases": [asdict(case) for case in QUERY_CASES],
        "by_model": {},
    }
    for model in models:
        model_rows = [row for row in results if row.model == model]
        model_summary = metric_summary(model_rows)
        model_summary["by_complexity"] = {
            complexity: metric_summary([row for row in model_rows if row.complexity == complexity])
            for complexity in ("simple", "medium", "complex")
        }
        summary["by_model"][model] = model_summary
    return summary


def format_number(value: Any) -> str:
    return "-" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value)


def print_summary(summary: dict[str, Any]) -> None:
    print("\n=== 模型汇总（ms） ===")
    headers = ["model", "ok/runs", "TTFT avg", "TTFT p95", "total avg", "total p95", "JSON", "2 expansions"]
    rows: list[list[str]] = []
    for model, data in summary["by_model"].items():
        rows.append(
            [
                model,
                f"{data['successful_runs']}/{data['runs']}",
                format_number(data["ttft_ms"]["mean"]),
                format_number(data["ttft_ms"]["p95"]),
                format_number(data["total_ms"]["mean"]),
                format_number(data["total_ms"]["p95"]),
                f"{data['json_valid_rate']:.0%}",
                f"{data['exactly_two_expansions_rate']:.0%}",
            ]
        )

    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


def write_results(results: list[RunResult], summary: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"qwen_query_rewrite_runs_{timestamp}.csv"
    json_path = output_dir / f"qwen_query_rewrite_summary_{timestamp}.json"

    fieldnames = list(RunResult.__dataclass_fields__.keys())
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(asdict(row))

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    return csv_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Qwen models for Nalook query rewriting")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="待测试的模型 ID")
    parser.add_argument("--repeats", type=int, default=2, help="每个 Query/模型重复次数，默认 2")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次请求超时秒数")
    parser.add_argument("--max-tokens", type=int, default=160, help="最大输出 Token")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="百炼 OpenAI 兼容 API Base URL")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "benchmark_results",
        help="CSV/JSON 结果目录",
    )
    parser.add_argument("--no-warmup", action="store_true", help="禁用每个模型的预热请求")
    parser.add_argument("--seed", type=int, default=20260817, help="测试顺序随机种子")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = API_KEY.strip() or os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("错误：请设置 DASHSCOPE_API_KEY。", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print("错误：--repeats 必须 >= 1", file=sys.stderr)
        return 2

    print(f"模型：{', '.join(args.models)}")
    print(f"Query：{len(QUERY_CASES)} 条，每组重复 {args.repeats} 次")
    print(f"预计计费请求：{len(args.models) * len(QUERY_CASES) * args.repeats} 次（不含预热）")

    if not args.no_warmup:
        print("\n=== 预热（不计入统计） ===")
        warmup_case = QUERY_CASES[0]
        for model in args.models:
            result = run_streaming_request(
                api_key=api_key,
                base_url=args.base_url,
                model=model,
                case=warmup_case,
                repeat=0,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
            )
            state = "OK" if result.success else f"FAIL: {result.error[:120]}"
            print(f"{model}: {state}")

    jobs = [
        (repeat, case, model)
        for repeat in range(1, args.repeats + 1)
        for case in QUERY_CASES
        for model in args.models
    ]
    random.Random(args.seed).shuffle(jobs)

    results: list[RunResult] = []
    print("\n=== 正式测试 ===")
    for index, (repeat, case, model) in enumerate(jobs, start=1):
        print(
            f"[{index:>3}/{len(jobs)}] {model:<18} {case.query_id} repeat={repeat} ... ",
            end="",
            flush=True,
        )
        result = run_streaming_request(
            api_key=api_key,
            base_url=args.base_url,
            model=model,
            case=case,
            repeat=repeat,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
        )
        results.append(result)
        if result.success:
            print(
                f"TTFT={format_number(result.ttft_ms)}ms total={format_number(result.total_ms)}ms "
                f"json={'Y' if result.json_valid else 'N'} exp2={'Y' if result.exactly_two_expansions else 'N'}"
            )
        else:
            print(f"FAIL {result.error[:180]}")

    summary = build_summary(results, args.models, args.repeats)
    print_summary(summary)
    csv_path, json_path = write_results(results, summary, args.output_dir)
    print(f"\n逐条结果：{csv_path}")
    print(f"汇总结果：{json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
