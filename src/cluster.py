"""把 tags.csv 中的标签编码为可用于近邻搜索和聚类的向量。

默认处理全部标签；可以用 --limit 100 先做小规模测试。
输出的 tags_clean*.csv 与 tag_embeddings*.npy 严格按行对应。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

# Conda 环境中的 TensorFlow 与 Pandas/PyArrow 在部分 Apple Silicon 机器上
# 会触发 Abseil mutex 死锁。本脚本只使用 PyTorch，因此在导入
# Transformers 之前明确关闭 TensorFlow 后端探测。
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer


MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Encode tags into normalized embeddings")
    parser.add_argument("--input", type=Path, default=None, help="tags.csv 的路径")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录；默认与脚本同目录",
    )
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个标签；0 表示全部")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=64)
    parser.add_argument("--dimension", type=int, default=1024, choices=[128, 256, 512, 768, 1024])
    parser.add_argument("--suffix", default="", help="测试输出文件后缀，例如 _test")
    parser.add_argument("--query-tag", default="历史", help="完成后查看该标签的近邻")
    parser.add_argument("--reuse-tags", type=Path, default=None)
    parser.add_argument("--reuse-embeddings", type=Path, default=None)
    parser.add_argument(
        "--force-reencode",
        action="store_true",
        help="忽略已有进度和可复用向量，全部重新编码",
    )
    return parser.parse_args()


def choose_device() -> tuple[str, torch.dtype]:
    if torch.backends.mps.is_available():
        return "mps", torch.float16
    if torch.cuda.is_available():
        return "cuda", torch.float16
    return "cpu", torch.float32


def last_token_pool(last_hidden_states: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    """Qwen3-Embedding 官方模型卡所使用的 last-token pooling。"""
    left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
    if left_padding:
        return last_hidden_states[:, -1]

    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch_size = last_hidden_states.shape[0]
    row_indices = torch.arange(batch_size, device=last_hidden_states.device)
    return last_hidden_states[row_indices, sequence_lengths]


def read_tags(csv_path: Path, limit: int) -> list[str]:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    if "tag" not in df.columns:
        raise ValueError(f"{csv_path} 中找不到 tag 列，实际列名为：{list(df.columns)}")

    series = df["tag"].dropna().astype(str).str.strip()
    tags = series[series.ne("")].drop_duplicates().tolist()
    if limit > 0:
        tags = tags[:limit]
    return tags


def dataset_fingerprint(tags: list[str], dimension: int) -> str:
    digest = hashlib.sha256()
    digest.update(MODEL_NAME.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(dimension).encode("ascii"))
    for tag in tags:
        digest.update(b"\0")
        digest.update(tag.encode("utf-8"))
    return digest.hexdigest()


def load_resume_position(
    progress_path: Path,
    *,
    fingerprint: str,
    new_tag_count: int,
) -> int | None:
    if not progress_path.exists():
        return None
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if (
        progress.get("dataset_fingerprint") != fingerprint
        or progress.get("new_tag_count") != new_tag_count
    ):
        return None
    return int(progress.get("completed_new_tags", 0))


def write_progress(
    progress_path: Path,
    *,
    completed_new_tags: int,
    tag_count: int,
    new_tag_count: int,
    reused_tag_count: int,
    dimension: int,
    fingerprint: str,
) -> None:
    progress_path.write_text(
        json.dumps(
            {
                "completed_new_tags": completed_new_tags,
                "tag_count": tag_count,
                "new_tag_count": new_tag_count,
                "reused_tag_count": reused_tag_count,
                "dimension": dimension,
                "model": MODEL_NAME,
                "dataset_fingerprint": fingerprint,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def reusable_embedding_rows(
    tags: list[str],
    reuse_tags_path: Path | None,
    reuse_embeddings_path: Path | None,
    dimension: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """返回新数据行号、旧数据行号以及旧 Embedding；只按完全相同 Tag 复用。"""
    if reuse_tags_path is None and reuse_embeddings_path is None:
        return (
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.int64),
            None,
        )
    if reuse_tags_path is None or reuse_embeddings_path is None:
        raise ValueError("--reuse-tags 和 --reuse-embeddings 必须同时提供")
    if not reuse_tags_path.exists() or not reuse_embeddings_path.exists():
        print("复用文件不存在，将全部重新编码", flush=True)
        return (
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.int64),
            None,
        )

    previous_tags = pd.read_csv(reuse_tags_path, encoding="utf-8-sig")
    if "tag" not in previous_tags.columns:
        raise ValueError(f"{reuse_tags_path} 缺少 tag 列")
    previous_embeddings = np.load(reuse_embeddings_path, mmap_mode="r")
    if (
        previous_embeddings.ndim != 2
        or len(previous_tags) != len(previous_embeddings)
        or previous_embeddings.shape[1] != dimension
    ):
        print("旧 Tag/Embedding 尺寸不匹配，将全部重新编码", flush=True)
        return (
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.int64),
            None,
        )

    old_positions = {
        str(tag): position
        for position, tag in enumerate(previous_tags["tag"].astype(str))
    }
    new_rows: list[int] = []
    old_rows: list[int] = []
    for new_row, tag in enumerate(tags):
        old_row = old_positions.get(tag)
        if old_row is not None:
            new_rows.append(new_row)
            old_rows.append(old_row)
    return (
        np.asarray(new_rows, dtype=np.int64),
        np.asarray(old_rows, dtype=np.int64),
        previous_embeddings,
    )


def show_similar_tags(
    tags: list[str],
    embeddings: np.ndarray,
    query_tag: str,
    top_k: int = 20,
) -> None:
    if query_tag not in tags:
        print(f"\n没有找到用于检查的标签：{query_tag}")
        return

    query_index = tags.index(query_tag)
    scores = embeddings @ embeddings[query_index]
    candidate_count = min(top_k + 1, len(tags))
    top_indices = np.argpartition(scores, -candidate_count)[-candidate_count:]
    top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

    print(f"\n与“{query_tag}”最相似的 {top_k} 个标签：")
    shown = 0
    for index in top_indices:
        if int(index) == query_index:
            continue
        print(f"{tags[int(index)]:30s} {float(scores[int(index)]):.4f}")
        shown += 1
        if shown >= top_k:
            break


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else script_dir
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = args.input.resolve() if args.input else script_dir / "tags.csv"
    tags_path = output_dir / f"tags_clean{args.suffix}.csv"
    embeddings_path = output_dir / f"tag_embeddings{args.suffix}.npy"
    progress_path = output_dir / f"embedding_progress{args.suffix}.json"
    manifest_path = output_dir / f"embedding_manifest{args.suffix}.json"

    tags = read_tags(input_path, args.limit)
    if not tags:
        raise ValueError("清洗后没有可编码的标签")

    pd.DataFrame({"tag_id": np.arange(len(tags)), "tag": tags}).to_csv(
        tags_path,
        index=False,
        encoding="utf-8-sig",
    )
    print(f"读取到 {len(tags):,} 个标签", flush=True)

    fingerprint = dataset_fingerprint(tags, args.dimension)
    if args.force_reencode:
        reused_new_rows = np.empty(0, dtype=np.int64)
        reused_old_rows = np.empty(0, dtype=np.int64)
        previous_embeddings = None
    else:
        reused_new_rows, reused_old_rows, previous_embeddings = (
            reusable_embedding_rows(
                tags,
                args.reuse_tags.expanduser().resolve()
                if args.reuse_tags
                else None,
                args.reuse_embeddings.expanduser().resolve()
                if args.reuse_embeddings
                else None,
                args.dimension,
            )
        )
    reused_mask = np.zeros(len(tags), dtype=bool)
    reused_mask[reused_new_rows] = True
    encode_rows = np.flatnonzero(~reused_mask)

    resume_position: int | None = None
    if not args.force_reencode:
        resume_position = load_resume_position(
            progress_path,
            fingerprint=fingerprint,
            new_tag_count=len(encode_rows),
        )
    completed = resume_position or 0
    if resume_position is not None and embeddings_path.exists():
        embeddings = np.lib.format.open_memmap(embeddings_path, mode="r+")
        expected_shape = (len(tags), args.dimension)
        if embeddings.shape != expected_shape:
            print("已有向量文件尺寸不匹配，将从头计算", flush=True)
            completed = 0
            resume_position = None
            embeddings = np.lib.format.open_memmap(
                embeddings_path,
                mode="w+",
                dtype=np.float32,
                shape=expected_shape,
            )
        elif completed > 0:
            print(
                f"从第 {completed:,}/{len(encode_rows):,} 个新 Tag 继续",
                flush=True,
            )
        else:
            print("已有向量与数据指纹一致，无需重新初始化", flush=True)
    else:
        completed = 0
        embeddings = np.lib.format.open_memmap(
            embeddings_path,
            mode="w+",
            dtype=np.float32,
            shape=(len(tags), args.dimension),
        )
        if previous_embeddings is not None and len(reused_new_rows):
            copy_batch_size = 20_000
            for start in range(0, len(reused_new_rows), copy_batch_size):
                end = min(start + copy_batch_size, len(reused_new_rows))
                embeddings[reused_new_rows[start:end]] = np.asarray(
                    previous_embeddings[reused_old_rows[start:end]],
                    dtype=np.float32,
                )
            embeddings.flush()

    print(
        f"复用旧向量 {len(reused_new_rows):,} 个；"
        f"需要新编码 {len(encode_rows):,} 个",
        flush=True,
    )

    remaining_rows = encode_rows[completed:]
    if len(remaining_rows):
        device, dtype = choose_device()
        print(f"加载模型 {MODEL_NAME}；device={device}，dtype={dtype}", flush=True)

        tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, padding_side="left")
        model = AutoModel.from_pretrained(MODEL_NAME, torch_dtype=dtype)
        model.to(device)
        model.eval()

    with torch.inference_mode():
        for start in range(0, len(remaining_rows), args.batch_size):
            end = min(start + args.batch_size, len(remaining_rows))
            batch_rows = remaining_rows[start:end]
            batch = tokenizer(
                [tags[int(row)] for row in batch_rows],
                padding=True,
                truncation=True,
                max_length=args.max_length,
                return_tensors="pt",
            )
            batch = {name: tensor.to(device) for name, tensor in batch.items()}
            output = model(**batch)
            batch_embeddings = last_token_pool(output.last_hidden_state, batch["attention_mask"])
            batch_embeddings = batch_embeddings[:, : args.dimension]
            batch_embeddings = F.normalize(batch_embeddings, p=2, dim=1)

            embeddings[batch_rows] = batch_embeddings.float().cpu().numpy()
            embeddings.flush()
            completed_total = completed + end
            write_progress(
                progress_path,
                completed_new_tags=completed_total,
                tag_count=len(tags),
                new_tag_count=len(encode_rows),
                reused_tag_count=len(reused_new_rows),
                dimension=args.dimension,
                fingerprint=fingerprint,
            )
            finished_total = len(reused_new_rows) + completed_total
            print(
                f"\r已完成 {finished_total:,}/{len(tags):,} "
                f"({finished_total / len(tags):.1%})",
                end="",
                flush=True,
            )

    write_progress(
        progress_path,
        completed_new_tags=len(encode_rows),
        tag_count=len(tags),
        new_tag_count=len(encode_rows),
        reused_tag_count=len(reused_new_rows),
        dimension=args.dimension,
        fingerprint=fingerprint,
    )
    print("\nEmbedding 生成完成", flush=True)
    print(f"标签：{tags_path}", flush=True)
    print(f"向量：{embeddings_path}，shape={embeddings.shape}", flush=True)
    manifest_path.write_text(
        json.dumps(
            {
                "model": MODEL_NAME,
                "dimension": args.dimension,
                "tag_count": len(tags),
                "reused_tag_count": len(reused_new_rows),
                "newly_encoded_tag_count": len(encode_rows),
                "dataset_fingerprint": fingerprint,
                "tags_file": str(tags_path),
                "embeddings_file": str(embeddings_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    show_similar_tags(tags, embeddings, args.query_tag)


if __name__ == "__main__":
    main()
