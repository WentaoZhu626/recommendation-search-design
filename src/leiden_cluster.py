"""用 HNSW 近邻图和 Leiden 对标签 Embedding 做社区发现。

当前默认参数用于主题社区发现：
    Top-K = 20
    cosine similarity >= 0.77
    只保留 mutual KNN 边
    resolution = 16

同一张图会连续测试多个 resolution；HNSW 索引和图边会缓存，后续只调
resolution 时无需重新做近邻搜索。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

# igraph 会探测 Matplotlib；把缓存限定在脚本目录，避免 macOS/Conda
# 环境因为默认字体缓存目录不可写而反复重建缓存。
os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parent / ".matplotlib_cache"),
)

import hnswlib
import igraph as ig
import leidenalg
import numpy as np
import pandas as pd


CLEANING_VERSION = "v1"

# 这里只放“完整等于该词时没有独立 Tag 价值”的词。不要做子串匹配，
# 否则 on 会误伤 London、iPhone、OnePlus 等正常 Tag。
TAG_BLOCKLIST = {
    "a",
    "an",
    "the",
    "in",
    "on",
    "at",
    "of",
    "to",
    "by",
    "for",
    "from",
    "and",
    "or",
    "but",
    "any",
    "some",
    "other",
    "another",
    "how",
    "could",
    "should",
    "would",
    "under",
    "none",
    "null",
    "nan",
    "unknown",
    "其他",
    "其它",
    "未知",
}

# 短词白名单优先于黑名单和单字母规则。
TAG_ALLOWLIST = {
    "AI",
    "AR",
    "VR",
    "IT",
    "AM",
    "GO",
    "F1",
    "PS4",
    "PS5",
    "B站",
    "X",
    "On",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HNSW + Leiden tag clustering")
    parser.add_argument("--tags", type=Path, default=None, help="tags_clean.csv")
    parser.add_argument("--embeddings", type=Path, default=None, help="tag_embeddings.npy")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="缓存和结果根目录；默认与脚本同目录",
    )
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条；0 表示全量")
    parser.add_argument("--suffix", default="", help="缓存和输出文件后缀，例如 _test")

    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--similarity-threshold", type=float, default=0.77)
    parser.add_argument(
        "--mutual",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否只保留互为 Top-K 的边",
    )
    parser.add_argument("--query-batch-size", type=int, default=10_000)

    parser.add_argument("--hnsw-m", type=int, default=32)
    parser.add_argument("--ef-construction", type=int, default=200)
    parser.add_argument("--ef-search", type=int, default=120)
    parser.add_argument("--threads", type=int, default=-1)

    parser.add_argument(
        "--resolutions",
        type=float,
        nargs="+",
        default=[16.0],
        help="一次测试一个或多个 Leiden resolution",
    )
    parser.add_argument("--iterations", type=int, default=-1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--representatives", type=int, default=15)
    parser.add_argument(
        "--clean-tags",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否在建图前过滤停用词、纯标点和无意义单字母 Tag",
    )
    parser.add_argument(
        "--visualize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否生成最大社区 HTML 可视化",
    )
    parser.add_argument("--visual-top-communities", type=int, default=25)

    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--rebuild-graph", action="store_true")
    return parser.parse_args()


def float_label(value: float) -> str:
    return f"{value:g}".replace("-", "m").replace(".", "p")


def normalize_tag(tag: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(tag))
    return re.sub(r"\s+", " ", normalized).strip()


def validate_tag(tag: object) -> tuple[str | None, str | None]:
    normalized = normalize_tag(tag)
    if not normalized:
        return None, "empty"
    if normalized in TAG_ALLOWLIST:
        return normalized, None
    if normalized.lower() in TAG_BLOCKLIST:
        return None, "stopword"
    if re.fullmatch(r"[\W_]+", normalized):
        return None, "punctuation_only"
    if re.fullmatch(r"[A-Za-z]", normalized):
        return None, "single_ascii_letter"
    return normalized, None


def clean_tags_and_embeddings(
    tags: pd.DataFrame,
    embeddings: np.ndarray,
    report_path: Path,
) -> tuple[pd.DataFrame, np.ndarray]:
    kept_positions: list[int] = []
    normalized_tags: list[str] = []
    rejected: list[dict] = []
    seen: set[str] = set()
    source_ids = tags["source_tag_id"].to_numpy()

    for position, raw_tag in enumerate(tags["tag"].tolist()):
        cleaned_tag, reason = validate_tag(raw_tag)
        if cleaned_tag is not None and cleaned_tag in seen:
            cleaned_tag, reason = None, "duplicate_after_normalization"
        if cleaned_tag is None:
            rejected.append(
                {
                    "source_tag_id": int(source_ids[position]),
                    "raw_tag": raw_tag,
                    "reason": reason,
                }
            )
            continue
        seen.add(cleaned_tag)
        kept_positions.append(position)
        normalized_tags.append(cleaned_tag)

    cleaned = tags.iloc[kept_positions].copy().reset_index(drop=True)
    cleaned["tag"] = normalized_tags
    cleaned["tag_id"] = np.arange(len(cleaned), dtype=np.int64)
    cleaned_embeddings = np.asarray(
        embeddings[np.asarray(kept_positions, dtype=np.int64)],
        dtype=np.float32,
    )

    rejected_df = pd.DataFrame(
        rejected,
        columns=["source_tag_id", "raw_tag", "reason"],
    )
    rejected_df.to_csv(report_path, index=False, encoding="utf-8-sig")
    print(
        f"Tag 清洗：保留 {len(cleaned):,}，过滤 {len(rejected_df):,}；"
        f"报告={report_path}",
        flush=True,
    )
    return cleaned, cleaned_embeddings


def validate_inputs(tags: pd.DataFrame, embeddings: np.ndarray) -> None:
    if "tag" not in tags.columns:
        raise ValueError(f"标签文件缺少 tag 列，实际列名：{list(tags.columns)}")
    if embeddings.ndim != 2:
        raise ValueError(f"Embedding 必须是二维矩阵，实际 shape={embeddings.shape}")
    if len(tags) != len(embeddings):
        raise ValueError(f"标签数 {len(tags)} 与向量数 {len(embeddings)} 不一致")
    if not np.isfinite(np.asarray(embeddings[:: max(1, len(embeddings) // 1000)])).all():
        raise ValueError("Embedding 样本中存在 NaN 或 Inf")


def dataset_fingerprint(
    tags: pd.DataFrame,
    embeddings: np.ndarray,
    embeddings_path: Path,
) -> str:
    digest = hashlib.sha256()
    digest.update(str(embeddings.shape).encode("ascii"))
    digest.update(b"\0")
    for row in tags[["source_tag_id", "tag"]].itertuples(index=False):
        digest.update(str(int(row.source_tag_id)).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(row.tag).encode("utf-8"))
        digest.update(b"\0")
    stat = embeddings_path.stat()
    digest.update(str(stat.st_size).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def cache_metadata_matches(path: Path, expected: dict) -> bool:
    if not path.exists():
        return False
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return actual == expected


def load_or_build_hnsw(
    embeddings: np.ndarray,
    index_path: Path,
    metadata_path: Path,
    fingerprint: str,
    args: argparse.Namespace,
) -> hnswlib.Index:
    count, dimension = embeddings.shape
    metadata = {
        "count": count,
        "dimension": dimension,
        "space": "cosine",
        "hnsw_m": args.hnsw_m,
        "ef_construction": args.ef_construction,
        "seed": args.seed,
        "dataset_fingerprint": fingerprint,
    }

    index = hnswlib.Index(space="cosine", dim=dimension)
    can_reuse = (
        not args.rebuild_index
        and index_path.exists()
        and cache_metadata_matches(metadata_path, metadata)
    )

    if can_reuse:
        print(f"加载已有 HNSW 索引：{index_path}", flush=True)
        index.load_index(str(index_path), max_elements=count)
    else:
        print(
            f"构建 HNSW：n={count:,}, dim={dimension}, "
            f"M={args.hnsw_m}, efConstruction={args.ef_construction}",
            flush=True,
        )
        index.init_index(
            max_elements=count,
            ef_construction=args.ef_construction,
            M=args.hnsw_m,
            random_seed=args.seed,
        )
        index.add_items(
            embeddings,
            np.arange(count, dtype=np.int64),
            num_threads=args.threads,
        )
        index.save_index(str(index_path))
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"HNSW 索引已保存：{index_path}", flush=True)

    index.set_ef(max(args.ef_search, args.top_k + 1))
    index.set_num_threads(args.threads)
    return index


def deduplicate_undirected_edges(
    sources: np.ndarray,
    targets: np.ndarray,
    similarities: np.ndarray,
    vertex_count: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lower = np.minimum(sources, targets).astype(np.int64, copy=False)
    upper = np.maximum(sources, targets).astype(np.int64, copy=False)
    edge_keys = lower * vertex_count + upper

    order = np.argsort(edge_keys)
    sorted_keys = edge_keys[order]
    group_starts_mask = np.empty(len(sorted_keys), dtype=bool)
    group_starts_mask[0] = True
    group_starts_mask[1:] = sorted_keys[1:] != sorted_keys[:-1]
    group_starts = np.flatnonzero(group_starts_mask)

    sorted_similarities = similarities[order]
    max_similarities = np.maximum.reduceat(sorted_similarities, group_starts)
    chosen = order[group_starts]

    return (
        lower[chosen].astype(np.int32),
        upper[chosen].astype(np.int32),
        max_similarities.astype(np.float32),
    )


def build_graph_edges(
    index: hnswlib.Index,
    embeddings: np.ndarray,
    args: argparse.Namespace,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    count = len(embeddings)
    query_k = min(args.top_k + 1, count)
    source_parts: list[np.ndarray] = []
    target_parts: list[np.ndarray] = []
    similarity_parts: list[np.ndarray] = []

    print(
        f"查询近邻：Top-{args.top_k}, cosine>={args.similarity_threshold}, "
        f"mutual={args.mutual}",
        flush=True,
    )

    for start in range(0, count, args.query_batch_size):
        end = min(start + args.query_batch_size, count)
        labels, distances = index.knn_query(
            embeddings[start:end],
            k=query_k,
            num_threads=args.threads,
        )

        batch_sources = np.repeat(
            np.arange(start, end, dtype=np.int64),
            query_k,
        )
        batch_targets = labels.reshape(-1).astype(np.int64, copy=False)
        batch_similarities = 1.0 - distances.reshape(-1)

        keep = (
            (batch_sources != batch_targets)
            & (batch_similarities >= args.similarity_threshold)
        )
        source_parts.append(batch_sources[keep])
        target_parts.append(batch_targets[keep])
        similarity_parts.append(batch_similarities[keep].astype(np.float32))
        print(f"\r近邻查询 {end:,}/{count:,} ({end / count:.1%})", end="", flush=True)

    print(flush=True)
    sources = np.concatenate(source_parts)
    targets = np.concatenate(target_parts)
    similarities = np.concatenate(similarity_parts)
    print(f"阈值过滤后有向候选边：{len(sources):,}", flush=True)

    if args.mutual and len(sources):
        directed_keys = sources * count + targets
        reverse_keys = targets * count + sources
        sorted_keys = np.sort(directed_keys)
        positions = np.searchsorted(sorted_keys, reverse_keys)
        safe_positions = np.minimum(positions, len(sorted_keys) - 1)
        mutual_mask = (
            (positions < len(sorted_keys))
            & (sorted_keys[safe_positions] == reverse_keys)
        )
        sources = sources[mutual_mask]
        targets = targets[mutual_mask]
        similarities = similarities[mutual_mask]
        print(f"Mutual KNN 过滤后有向边：{len(sources):,}", flush=True)

    if not len(sources):
        return (
            np.empty(0, dtype=np.int32),
            np.empty(0, dtype=np.int32),
            np.empty(0, dtype=np.float32),
        )

    sources, targets, similarities = deduplicate_undirected_edges(
        sources,
        targets,
        similarities,
        count,
    )
    print(f"最终无向边：{len(sources):,}", flush=True)
    return sources, targets, similarities


def load_or_build_edges(
    index: hnswlib.Index,
    embeddings: np.ndarray,
    cache_path: Path,
    args: argparse.Namespace,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if cache_path.exists() and not args.rebuild_graph:
        print(f"加载已有近邻图：{cache_path}", flush=True)
        cached = np.load(cache_path)
        return cached["sources"], cached["targets"], cached["weights"]

    sources, targets, weights = build_graph_edges(index, embeddings, args)
    np.savez(cache_path, sources=sources, targets=targets, weights=weights)
    print(f"近邻图已保存：{cache_path}", flush=True)
    return sources, targets, weights


def size_bucket_counts(sizes: np.ndarray) -> dict[str, int]:
    return {
        "1": int(np.sum(sizes == 1)),
        "2-5": int(np.sum((sizes >= 2) & (sizes <= 5))),
        "6-10": int(np.sum((sizes >= 6) & (sizes <= 10))),
        "11-50": int(np.sum((sizes >= 11) & (sizes <= 50))),
        "51-100": int(np.sum((sizes >= 51) & (sizes <= 100))),
        "101-500": int(np.sum((sizes >= 101) & (sizes <= 500))),
        "500+": int(np.sum(sizes > 500)),
    }


def write_community_visualization(
    representatives: pd.DataFrame,
    output_path: Path,
    resolution: float,
    total_tags: int,
    singleton_count: int,
    top_n: int,
) -> None:
    top = representatives.head(max(1, top_n)).copy()
    records = []
    for row in top.itertuples(index=False):
        records.append(
            {
                "community_id": int(row.community_id),
                "community_size": int(row.community_size),
                "tags": str(row.representative_tags).split(" | "),
            }
        )

    payload = json.dumps(records, ensure_ascii=False)
    default_visible = min(12, len(records))
    maximum_visible = max(1, len(records))
    title = f"Tag 社区概览 · Resolution {resolution:g}"

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  color-scheme: light dark;
  --bg: #f6f7fb;
  --panel: #ffffff;
  --text: #182033;
  --muted: #667085;
  --border: #dfe3eb;
  --bar: #5078e8;
  --bar-soft: #dfe8ff;
  --tag: #eef2ff;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #111521;
    --panel: #191f2d;
    --text: #eef2ff;
    --muted: #aeb7ca;
    --border: #30394b;
    --bar: #86a3ff;
    --bar-soft: #263453;
    --tag: #252f49;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 28px;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
main {{ max-width: 1180px; margin: 0 auto; }}
h1 {{ margin: 0 0 8px; font-size: 26px; font-weight: 650; }}
.subtitle {{ color: var(--muted); margin-bottom: 20px; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
.stat {{
  min-width: 150px;
  padding: 13px 16px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
}}
.stat strong {{ display: block; font-size: 21px; }}
.stat span {{ color: var(--muted); font-size: 13px; }}
.controls {{
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 12px 0 16px;
}}
input[type="range"] {{ width: 230px; }}
#communities {{ display: grid; gap: 11px; }}
.community {{
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
}}
.row {{
  display: grid;
  grid-template-columns: 92px minmax(130px, 1fr) 72px;
  gap: 12px;
  align-items: center;
}}
.community-id {{ font-weight: 650; }}
.bar-track {{
  height: 10px;
  background: var(--bar-soft);
  border-radius: 999px;
  overflow: hidden;
}}
.bar {{ height: 100%; background: var(--bar); border-radius: inherit; }}
.size {{ text-align: right; font-variant-numeric: tabular-nums; }}
.tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 11px; }}
.tag {{
  padding: 4px 8px;
  background: var(--tag);
  border-radius: 999px;
  font-size: 13px;
}}
@media (max-width: 620px) {{
  body {{ padding: 16px; }}
  .row {{ grid-template-columns: 74px minmax(90px, 1fr) 58px; gap: 8px; }}
  .controls {{ align-items: flex-start; flex-direction: column; }}
}}
</style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <div class="subtitle">按社区规模排序；每个社区仅展示代表性 Tag。</div>
  <section class="stats" aria-label="社区统计">
    <div class="stat"><strong>{total_tags:,}</strong><span>清洗后 Tag</span></div>
    <div class="stat"><strong>{len(representatives):,}</strong><span>社区总数</span></div>
    <div class="stat"><strong>{singleton_count:,}</strong><span>孤立 Tag</span></div>
    <div class="stat"><strong>{int(representatives.iloc[0]["community_size"]):,}</strong><span>最大社区</span></div>
  </section>
  <div class="controls">
    <label for="visible-count">展示最大社区数量：<strong id="visible-value">{default_visible}</strong></label>
    <input id="visible-count" type="range" min="1" max="{maximum_visible}" value="{default_visible}">
  </div>
  <section id="communities" aria-label="最大社区列表"></section>
</main>
<script>
const data = {payload};
const root = document.getElementById("communities");
const range = document.getElementById("visible-count");
const value = document.getElementById("visible-value");
const maxSize = Math.max(...data.map(d => d.community_size), 1);

function render() {{
  const count = Number(range.value);
  value.textContent = String(count);
  root.replaceChildren();
  data.slice(0, count).forEach(item => {{
    const card = document.createElement("article");
    card.className = "community";

    const row = document.createElement("div");
    row.className = "row";

    const id = document.createElement("div");
    id.className = "community-id";
    id.textContent = `社区 ${{item.community_id}}`;

    const track = document.createElement("div");
    track.className = "bar-track";
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.width = `${{Math.max(2, item.community_size / maxSize * 100)}}%`;
    track.appendChild(bar);

    const size = document.createElement("div");
    size.className = "size";
    size.textContent = item.community_size.toLocaleString();
    row.append(id, track, size);

    const tags = document.createElement("div");
    tags.className = "tags";
    item.tags.forEach(text => {{
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = text;
      tags.appendChild(tag);
    }});

    card.append(row, tags);
    root.appendChild(card);
  }});
}}

range.addEventListener("input", render);
render();
</script>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")
    print(f"社区可视化：{output_path}", flush=True)


def run_leiden(
    graph: ig.Graph,
    tags: pd.DataFrame,
    resolution: float,
    args: argparse.Namespace,
    result_dir: Path,
    run_label: str,
) -> dict:
    print(f"\n运行 Leiden：resolution={resolution:g}", flush=True)
    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=resolution,
        n_iterations=args.iterations,
        seed=args.seed,
    )

    membership = np.asarray(partition.membership, dtype=np.int32)
    community_sizes = np.bincount(membership)
    strengths = np.asarray(graph.strength(weights="weight"), dtype=np.float32)
    vertex_sizes = community_sizes[membership]

    result = tags[["tag_id", "source_tag_id", "tag"]].copy()
    result["community_id"] = membership
    result["community_size"] = vertex_sizes
    result["weighted_degree"] = strengths
    result = result.sort_values(
        ["community_size", "community_id", "weighted_degree", "tag"],
        ascending=[False, True, False, True],
    )

    resolution_label = float_label(resolution)
    members_path = result_dir / f"tag_communities_{run_label}_r{resolution_label}.csv"
    result.to_csv(members_path, index=False, encoding="utf-8-sig")

    representatives = (
        result.sort_values(
            ["community_id", "weighted_degree", "tag"],
            ascending=[True, False, True],
        )
        .groupby("community_id", sort=False)
        .agg(
            community_size=("community_size", "first"),
            representative_tags=(
                "tag",
                lambda values: " | ".join(values.head(args.representatives)),
            ),
        )
        .reset_index()
        .sort_values(["community_size", "community_id"], ascending=[False, True])
    )
    summary_path = result_dir / f"community_summary_{run_label}_r{resolution_label}.csv"
    representatives.to_csv(summary_path, index=False, encoding="utf-8-sig")

    buckets = size_bucket_counts(community_sizes)
    visualization_path = (
        result_dir / f"largest_communities_{run_label}_r{resolution_label}.html"
    )
    if args.visualize:
        write_community_visualization(
            representatives=representatives,
            output_path=visualization_path,
            resolution=resolution,
            total_tags=len(tags),
            singleton_count=buckets["1"],
            top_n=args.visual_top_communities,
        )

    statistics = {
        "resolution": resolution,
        "community_count": int(len(community_sizes)),
        "singleton_communities": buckets["1"],
        "largest_community": int(community_sizes.max()),
        "median_community_size": float(np.median(community_sizes)),
        "p90_community_size": float(np.quantile(community_sizes, 0.90)),
        "p99_community_size": float(np.quantile(community_sizes, 0.99)),
        "quality": float(partition.quality()),
        **{f"communities_{name}": count for name, count in buckets.items()},
        "members_file": str(members_path),
        "summary_file": str(summary_path),
        "visualization_file": str(visualization_path) if args.visualize else "",
    }

    print(
        f"社区数={statistics['community_count']:,}，"
        f"单例={statistics['singleton_communities']:,}，"
        f"最大社区={statistics['largest_community']:,}，"
        f"中位数={statistics['median_community_size']:g}",
        flush=True,
    )
    print(f"成员结果：{members_path}", flush=True)
    print(f"社区摘要：{summary_path}", flush=True)
    return statistics


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    work_dir = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else script_dir
    )
    tags_path = args.tags.resolve() if args.tags else script_dir / "tags_clean.csv"
    embeddings_path = (
        args.embeddings.resolve()
        if args.embeddings
        else script_dir / "tag_embeddings.npy"
    )

    cache_dir = work_dir / "leiden_cache"
    result_dir = work_dir / "leiden_results"
    cache_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    tags = pd.read_csv(tags_path, encoding="utf-8-sig")
    embeddings = np.load(embeddings_path, mmap_mode="r")
    validate_inputs(tags, embeddings)

    if "tag_id" in tags.columns:
        tags["source_tag_id"] = tags["tag_id"].astype(np.int64)
    else:
        tags["source_tag_id"] = np.arange(len(tags), dtype=np.int64)

    if args.limit > 0:
        tags = tags.iloc[: args.limit].copy()
        embeddings = embeddings[: args.limit]

    cleaning_report_path = (
        result_dir / f"filtered_tags_{CLEANING_VERSION}{args.suffix}.csv"
    )
    if args.clean_tags:
        tags, embeddings = clean_tags_and_embeddings(
            tags,
            embeddings,
            cleaning_report_path,
        )
        cleaning_label = f"clean{CLEANING_VERSION}"
    else:
        tags = tags.copy().reset_index(drop=True)
        tags["tag"] = tags["tag"].map(normalize_tag)
        tags["tag_id"] = np.arange(len(tags), dtype=np.int64)
        cleaning_label = "raw"

    print(
        f"输入：{len(tags):,} 个标签，Embedding shape={embeddings.shape}",
        flush=True,
    )

    fingerprint = dataset_fingerprint(tags, embeddings, embeddings_path)
    dataset_label = (
        f"n{len(tags)}_d{embeddings.shape[1]}_{cleaning_label}_"
        f"f{fingerprint[:12]}{args.suffix}"
    )
    index_path = cache_dir / f"hnsw_{dataset_label}.bin"
    index_meta_path = cache_dir / f"hnsw_{dataset_label}.json"

    mutual_label = "mutual" if args.mutual else "union"
    threshold_label = float_label(args.similarity_threshold)
    run_label = f"k{args.top_k}_s{threshold_label}_{mutual_label}{args.suffix}"
    graph_cache_path = cache_dir / f"graph_{dataset_label}_{run_label}.npz"

    index = load_or_build_hnsw(
        embeddings,
        index_path,
        index_meta_path,
        fingerprint,
        args,
    )
    sources, targets, weights = load_or_build_edges(
        index,
        embeddings,
        graph_cache_path,
        args,
    )

    print(
        f"构建 igraph：顶点={len(tags):,}，边={len(sources):,}",
        flush=True,
    )
    edge_array = np.column_stack((sources, targets))
    graph = ig.Graph(
        n=len(tags),
        edges=edge_array,
        directed=False,
        edge_attrs={"weight": weights.tolist()},
    )
    isolated_count = int(np.count_nonzero(np.asarray(graph.degree()) == 0))
    print(
        f"图中孤立 Tag：{isolated_count:,}",
        flush=True,
    )

    comparison = []
    for resolution in args.resolutions:
        comparison.append(
            run_leiden(
                graph,
                tags,
                resolution,
                args,
                result_dir,
                run_label,
            )
        )

    comparison_path = result_dir / f"resolution_comparison_{run_label}.csv"
    pd.DataFrame(comparison).to_csv(
        comparison_path,
        index=False,
        encoding="utf-8-sig",
    )
    print(f"\nResolution 对比：{comparison_path}", flush=True)


if __name__ == "__main__":
    main()
