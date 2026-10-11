#!/usr/bin/env python3
"""用真实 post_tags 快照模拟用户画像、一跳兴趣扩散和双通道推荐。

核心约束：
1. 一个帖子可以同时贡献给多个社区；
2. 初始兴趣与一跳扩散兴趣分别召回；
3. 默认 30 条推荐中固定保留 21 条初始兴趣、9 条扩散兴趣；
4. 只有某一通道候选不足时，另一通道才补位。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import unicodedata
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np
from scipy import sparse


DEFAULT_HISTORY_SEEDS = {
    "broad": [85, 70, 97, 19, 240],
    "shifting": [70, 85],
    "focused": [85],
}


@dataclass
class Community:
    community_id: int
    size: int
    post_count: int
    label: str
    representative_tags: str


def normalize_tag(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return " ".join(value.strip().split())


def iter_post_tags(path: Path) -> Iterator[tuple[int, list[str]]]:
    """流式读取按 post_id 排序的 CSV，一次只保留一个帖子的标签。"""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"post_id", "tag"}.issubset(reader.fieldnames):
            raise ValueError(f"{path} 必须包含 post_id 和 tag 两列")

        current_id: int | None = None
        current_tags: list[str] = []
        previous_id: int | None = None
        for row in reader:
            post_id = int(row["post_id"])
            if previous_id is not None and post_id < previous_id:
                raise ValueError(
                    "post_tags.csv 必须按 post_id 排序，当前文件出现逆序："
                    f"{previous_id} -> {post_id}"
                )
            previous_id = post_id
            tag = normalize_tag(row["tag"])
            if current_id is None:
                current_id = post_id
            if post_id != current_id:
                yield current_id, current_tags
                current_id = post_id
                current_tags = []
            if tag:
                current_tags.append(tag)
        if current_id is not None:
            yield current_id, current_tags


def find_latest_snapshot(project_dir: Path) -> Path:
    snapshots_dir = project_dir / "snapshots"
    candidates = sorted(
        (p for p in snapshots_dir.iterdir() if p.is_dir() and (p / "post_tags.csv").exists()),
        key=lambda p: p.name,
    )
    if not candidates:
        raise FileNotFoundError(f"{snapshots_dir} 下没有可用快照")
    return candidates[-1]


def load_communities(
    snapshot: Path,
) -> tuple[dict[str, int], list[Community], dict[int, int]]:
    leiden_path = (
        snapshot
        / "leiden_results"
        / "tag_communities_k20_s0p77_mutual_r16.csv"
    )
    nodes_path = snapshot / "community_transition_results" / "community_nodes.csv"

    tag_to_community: dict[str, int] = {}
    with leiden_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if int(row["community_size"]) <= 1:
                continue
            tag = normalize_tag(row["tag"])
            if tag:
                tag_to_community[tag] = int(row["community_id"])

    communities: list[Community] = []
    with nodes_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            community_id = int(row["community_id"])
            representatives = row["representative_tags"]
            short_tags = [x.strip() for x in representatives.split("|") if x.strip()][:3]
            communities.append(
                Community(
                    community_id=community_id,
                    size=int(row["community_size"]),
                    post_count=int(row["post_count"]),
                    label=" / ".join(short_tags),
                    representative_tags=representatives,
                )
            )
    communities.sort(key=lambda x: x.community_id)
    community_to_col = {c.community_id: i for i, c in enumerate(communities)}
    return tag_to_community, communities, community_to_col


def count_tag_document_frequency(
    post_tags_path: Path,
) -> tuple[Counter[str], int]:
    tag_df: Counter[str] = Counter()
    post_count = 0
    for post_count, (_, tags) in enumerate(iter_post_tags(post_tags_path), start=1):
        tag_df.update(set(tags))
        if post_count % 50_000 == 0:
            print(f"[1/4] 已统计 {post_count:,} 个帖子", flush=True)
    return tag_df, post_count


def compute_tag_quality(tag_df: Counter[str], post_count: int) -> dict[str, float]:
    qualities: dict[str, float] = {}
    for tag, df in tag_df.items():
        idf = math.log1p(post_count / (df + 5.0))
        reliability = math.sqrt(df / (df + 5.0))
        qualities[tag] = math.sqrt(idf) * reliability
    return qualities


def build_post_community_matrix(
    post_tags_path: Path,
    tag_to_community: dict[str, int],
    community_to_col: dict[int, int],
    tag_quality: dict[str, float],
    synonym_discount: float,
) -> tuple[np.ndarray, sparse.csr_matrix]:
    """构建帖子×社区稀疏矩阵；每个非空帖子行归一化为 1。"""
    post_ids = array("q")
    indices = array("i")
    values = array("f")
    indptr = array("q", [0])

    for row_number, (post_id, tags) in enumerate(iter_post_tags(post_tags_path), start=1):
        post_ids.append(post_id)
        unique_tags = set(tags)
        mapped: list[tuple[int, float]] = []
        for tag in unique_tags:
            community_id = tag_to_community.get(tag)
            if community_id is None:
                continue
            quality = tag_quality.get(tag, 0.0)
            if quality > 0:
                mapped.append((community_to_col[community_id], quality))

        if mapped:
            quality_sum = sum(q for _, q in mapped)
            by_community: dict[int, list[float]] = defaultdict(list)
            for community_col, quality in mapped:
                by_community[community_col].append(quality / quality_sum)

            raw_community_weights: dict[int, float] = {}
            for community_col, weights in by_community.items():
                largest = max(weights)
                raw_community_weights[community_col] = largest + synonym_discount * (
                    sum(weights) - largest
                )
            community_sum = sum(raw_community_weights.values())
            for community_col, weight in sorted(raw_community_weights.items()):
                indices.append(community_col)
                values.append(weight / community_sum)

        indptr.append(len(indices))
        if row_number % 50_000 == 0:
            print(f"[2/4] 已编码 {row_number:,} 个帖子", flush=True)

    matrix = sparse.csr_matrix(
        (
            np.frombuffer(values, dtype=np.float32),
            np.frombuffer(indices, dtype=np.int32),
            np.frombuffer(indptr, dtype=np.int64),
        ),
        shape=(len(post_ids), len(community_to_col)),
        dtype=np.float32,
    )
    return np.frombuffer(post_ids, dtype=np.int64).copy(), matrix


def load_transition_matrix(
    snapshot: Path,
    communities: list[Community],
    community_to_col: dict[int, int],
) -> sparse.csr_matrix:
    edge_path = (
        snapshot
        / "community_transition_results"
        / "community_transition_edges.csv"
    )
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    outgoing: set[int] = set()
    with edge_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            source_id = int(row["source_community_id"])
            target_id = int(row["target_community_id"])
            if source_id not in community_to_col or target_id not in community_to_col:
                continue
            source_col = community_to_col[source_id]
            rows.append(source_col)
            cols.append(community_to_col[target_id])
            data.append(float(row["transition_probability"]))
            outgoing.add(source_col)

    # 没有保留出边的死胡同社区采用自环，避免概率质量凭空消失。
    for col in range(len(communities)):
        if col not in outgoing:
            rows.append(col)
            cols.append(col)
            data.append(1.0)

    transition = sparse.csr_matrix(
        (np.asarray(data), (np.asarray(rows), np.asarray(cols))),
        shape=(len(communities), len(communities)),
        dtype=np.float64,
    )
    row_sums = np.asarray(transition.sum(axis=1)).ravel()
    transition = sparse.diags(1.0 / np.maximum(row_sums, 1e-12)) @ transition
    return transition.tocsr()


def strongest_posts_for_community(
    matrix_csc: sparse.csc_matrix,
    community_col: int,
    count: int,
    used: set[int],
    rng: random.Random,
) -> list[int]:
    column = matrix_csc.getcol(community_col)
    candidates = [
        (int(row), float(value))
        for row, value in zip(column.indices, column.data)
        if int(row) not in used
    ]
    if len(candidates) < count:
        raise RuntimeError(
            f"社区列 {community_col} 只有 {len(candidates)} 个可选帖子，无法抽取 {count} 个"
        )
    candidates.sort(key=lambda x: x[1], reverse=True)
    pool = candidates[: min(len(candidates), max(100, count * 20))]
    weights = [max(value, 1e-9) for _, value in pool]
    selected: list[int] = []
    while len(selected) < count:
        choice = rng.choices(pool, weights=weights, k=1)[0][0]
        if choice not in used and choice not in selected:
            selected.append(choice)
    used.update(selected)
    return selected


def simulate_histories(
    matrix: sparse.csr_matrix,
    community_to_col: dict[int, int],
    seed: int,
) -> dict[str, dict]:
    rng = random.Random(seed)
    matrix_csc = matrix.tocsc()
    histories: dict[str, dict] = {}

    used: set[int] = set()
    broad_rows: list[int] = []
    for community_id in DEFAULT_HISTORY_SEEDS["broad"]:
        broad_rows.extend(
            strongest_posts_for_community(
                matrix_csc, community_to_col[community_id], 2, used, rng
            )
        )
    rng.shuffle(broad_rows)
    histories["broad"] = {
        "description": "广泛浏览：历史、美食、艺术、数学、喜剧各抽取 2 帖并打乱",
        "history_rows": broad_rows,
        "seed_communities": DEFAULT_HISTORY_SEEDS["broad"],
    }

    used = set()
    old_rows = strongest_posts_for_community(
        matrix_csc,
        community_to_col[DEFAULT_HISTORY_SEEDS["shifting"][0]],
        5,
        used,
        rng,
    )
    new_rows = strongest_posts_for_community(
        matrix_csc,
        community_to_col[DEFAULT_HISTORY_SEEDS["shifting"][1]],
        5,
        used,
        rng,
    )
    histories["shifting"] = {
        "description": "兴趣转移：较早 5 帖偏美食，最近 5 帖偏历史",
        "history_rows": old_rows + new_rows,
        "seed_communities": DEFAULT_HISTORY_SEEDS["shifting"],
    }

    used = set()
    focused_rows = strongest_posts_for_community(
        matrix_csc,
        community_to_col[DEFAULT_HISTORY_SEEDS["focused"][0]],
        10,
        used,
        rng,
    )
    histories["focused"] = {
        "description": "兴趣集中：10 帖均以历史社区为主要兴趣",
        "history_rows": focused_rows,
        "seed_communities": DEFAULT_HISTORY_SEEDS["focused"],
    }
    return histories


def normalize_vector(vector: np.ndarray) -> np.ndarray:
    total = float(vector.sum())
    return vector / total if total > 0 else vector


def top_columns(vector: np.ndarray, count: int) -> np.ndarray:
    positive = np.flatnonzero(vector > 0)
    if len(positive) <= count:
        return positive[np.argsort(vector[positive])[::-1]]
    local = np.argpartition(vector[positive], -count)[-count:]
    chosen = positive[local]
    return chosen[np.argsort(vector[chosen])[::-1]]


def select_communities_with_mmr(
    relevance: np.ndarray,
    matrix: sparse.csr_matrix,
    count: int,
    mmr_lambda: float,
    candidate_pool_size: int,
) -> np.ndarray:
    """在高相关社区中用 MMR 去掉过于相似的社区。

    社区间相似度使用它们在所有帖子上的归属向量余弦相似度。例如
    “数学知识”和“比例关系”经常落在同一批帖子上，相似度就会较高。
    """
    positive = np.flatnonzero(relevance > 0)
    if len(positive) <= count:
        return positive[np.argsort(relevance[positive])[::-1]]

    pool_size = min(len(positive), max(count, candidate_pool_size))
    candidate_cols = top_columns(relevance, pool_size)
    # 避免为了多样性选到只有极小噪声权重的社区。
    minimum_relevance = float(relevance[candidate_cols[0]]) * 0.05
    candidate_cols = candidate_cols[relevance[candidate_cols] >= minimum_relevance]
    if len(candidate_cols) <= count:
        return candidate_cols

    candidate_matrix = matrix[:, candidate_cols]
    gram = (candidate_matrix.T @ candidate_matrix).toarray().astype(np.float64)
    norms = np.sqrt(np.maximum(np.diag(gram), 1e-12))
    similarities = gram / np.outer(norms, norms)
    np.fill_diagonal(similarities, 1.0)

    normalized_relevance = relevance[candidate_cols] / max(
        float(relevance[candidate_cols].max()), 1e-12
    )
    selected_local: list[int] = [int(np.argmax(normalized_relevance))]
    remaining = set(range(len(candidate_cols))) - set(selected_local)
    while remaining and len(selected_local) < count:
        best_local = max(
            remaining,
            key=lambda local: (
                mmr_lambda * float(normalized_relevance[local])
                - (1.0 - mmr_lambda)
                * max(
                    float(similarities[local, chosen])
                    for chosen in selected_local
                ),
                float(normalized_relevance[local]),
            ),
        )
        selected_local.append(best_local)
        remaining.remove(best_local)
    return candidate_cols[np.asarray(selected_local, dtype=np.int64)]


def build_user_profiles(
    matrix: sparse.csr_matrix,
    transition: sparse.csr_matrix,
    history_rows: list[int],
    half_life_posts: float,
    top_initial: int,
    top_expanded: int,
    mmr_lambda: float,
    mmr_candidate_pool: int,
) -> dict[str, np.ndarray]:
    history_matrix = matrix[history_rows]
    ages = np.arange(len(history_rows) - 1, -1, -1, dtype=np.float64)
    recency = np.power(2.0, -ages / half_life_posts)
    initial_full = np.asarray(recency @ history_matrix).ravel()
    initial_full = normalize_vector(initial_full)

    expanded_full = np.asarray(initial_full @ transition).ravel()
    # 扩散通道只保留用户历史尚未直接触达的新社区。
    expanded_full[initial_full > 0] = 0.0
    expanded_full = normalize_vector(expanded_full)

    initial_cols = select_communities_with_mmr(
        initial_full,
        matrix,
        top_initial,
        mmr_lambda,
        mmr_candidate_pool,
    )
    expanded_cols = select_communities_with_mmr(
        expanded_full,
        matrix,
        top_expanded,
        mmr_lambda,
        mmr_candidate_pool,
    )

    initial_selected = np.zeros_like(initial_full)
    initial_selected[initial_cols] = initial_full[initial_cols]
    initial_selected = normalize_vector(initial_selected)

    expanded_selected = np.zeros_like(expanded_full)
    expanded_selected[expanded_cols] = expanded_full[expanded_cols]
    expanded_selected = normalize_vector(expanded_selected)
    return {
        "initial_full": initial_full,
        "expanded_full": expanded_full,
        "initial_selected": initial_selected,
        "expanded_selected": expanded_selected,
        "initial_cols": initial_cols,
        "expanded_cols": expanded_cols,
        "recency": recency,
    }


def allocate_community_quotas(
    preference: np.ndarray,
    total: int,
    quota_power: float,
    max_share: float,
) -> dict[int, int]:
    """保证每个入选社区至少一条，再按软化后的兴趣权重分配剩余名额。"""
    cols = [int(col) for col in np.flatnonzero(preference > 0)]
    if not cols or total <= 0:
        return {}
    if total < len(cols):
        cols = sorted(cols, key=lambda col: preference[col], reverse=True)[:total]

    quotas = {col: 1 for col in cols}
    minimum_possible_cap = math.ceil(total / len(cols))
    cap = max(minimum_possible_cap, math.ceil(total * max_share))
    softened = {
        col: max(float(preference[col]), 1e-12) ** quota_power for col in cols
    }
    while sum(quotas.values()) < total:
        available = [col for col in cols if quotas[col] < cap]
        if not available:
            available = cols
        # D'Hondt 式逐席分配；开平方后的权重不会让头部社区垄断。
        chosen = max(
            available,
            key=lambda col: (softened[col] / (quotas[col] + 1), softened[col]),
        )
        quotas[chosen] += 1
    return quotas


def community_fair_schedule(quotas: dict[int, int]) -> list[int]:
    """把各社区名额均匀穿插，避免某个社区连续占据推荐列表开头。"""
    total = sum(quotas.values())
    used = Counter()
    schedule: list[int] = []
    for position in range(total):
        active = [col for col, quota in quotas.items() if used[col] < quota]
        chosen = max(
            active,
            key=lambda col: (
                quotas[col] * (position + 1) / total - used[col],
                quotas[col],
            ),
        )
        schedule.append(chosen)
        used[chosen] += 1
    return schedule


def select_posts_by_community(
    matrix_csc: sparse.csc_matrix,
    overall_scores: np.ndarray,
    preference: np.ndarray,
    total: int,
    excluded: set[int],
    quota_power: float,
    max_share: float,
) -> tuple[list[tuple[int, int]], dict[int, int]]:
    """先分社区名额，再在社区内部选帖；返回 (帖子行号, 分配社区列)。"""
    quotas = allocate_community_quotas(preference, total, quota_power, max_share)
    ranked_by_col: dict[int, list[int]] = {}
    cursors: dict[int, int] = {}
    max_overall = max(float(overall_scores.max(initial=0.0)), 1e-12)
    for col in quotas:
        column = matrix_csc.getcol(col)
        candidates: list[tuple[float, int]] = []
        for row, membership in zip(column.indices, column.data):
            row = int(row)
            if row in excluded:
                continue
            # 主要按帖子对指定社区的归属度排序，少量考虑整体用户匹配分。
            local_score = 0.8 * float(membership) + 0.2 * float(
                overall_scores[row] / max_overall
            )
            candidates.append((local_score, row))
        candidates.sort(reverse=True)
        ranked_by_col[col] = [row for _, row in candidates]
        cursors[col] = 0

    selected: list[tuple[int, int]] = []
    selected_rows: set[int] = set(excluded)
    realized = Counter()
    for assigned_col in community_fair_schedule(quotas):
        ranked = ranked_by_col[assigned_col]
        cursor = cursors[assigned_col]
        while cursor < len(ranked) and ranked[cursor] in selected_rows:
            cursor += 1
        cursors[assigned_col] = cursor + 1
        if cursor < len(ranked):
            row = ranked[cursor]
            selected.append((row, assigned_col))
            selected_rows.add(row)
            realized[assigned_col] += 1

    # 极少数社区候选不足时，从其他已选社区的剩余候选补位。
    while len(selected) < total:
        best: tuple[float, int, int] | None = None
        for col, ranked in ranked_by_col.items():
            cursor = cursors[col]
            while cursor < len(ranked) and ranked[cursor] in selected_rows:
                cursor += 1
            cursors[col] = cursor
            if cursor >= len(ranked):
                continue
            row = ranked[cursor]
            candidate = (float(overall_scores[row]), row, col)
            if best is None or candidate > best:
                best = candidate
        if best is None:
            break
        _, row, col = best
        cursors[col] += 1
        selected.append((row, col))
        selected_rows.add(row)
        realized[col] += 1
    return selected, dict(realized)


def interleave_channels(
    initial: list[tuple[int, int]],
    expanded: list[tuple[int, int]],
) -> list[tuple[int, str, int]]:
    """按 I-I-E 穿插两个已完成社区配额分配的通道。"""
    result: list[tuple[int, str, int]] = []
    initial_cursor = 0
    expanded_cursor = 0
    while initial_cursor < len(initial) or expanded_cursor < len(expanded):
        for _ in range(2):
            if initial_cursor < len(initial):
                row, col = initial[initial_cursor]
                result.append((row, "initial", col))
                initial_cursor += 1
        if expanded_cursor < len(expanded):
            row, col = expanded[expanded_cursor]
            result.append((row, "expanded", col))
            expanded_cursor += 1
    return result


def community_contributions(
    row: sparse.csr_matrix,
    preference: np.ndarray,
    communities: list[Community],
    limit: int = 3,
) -> str:
    contributions: list[tuple[float, int]] = []
    for col, value in zip(row.indices, row.data):
        contribution = float(value) * float(preference[col])
        if contribution > 0:
            contributions.append((contribution, int(col)))
    contributions.sort(reverse=True)
    return " | ".join(
        f"C{communities[col].community_id} {communities[col].label} ({value:.4f})"
        for value, col in contributions[:limit]
    )


def strongest_source_for_expansion(
    target_col: int,
    initial: np.ndarray,
    transition: sparse.csr_matrix,
    communities: list[Community],
) -> str:
    source_values = np.asarray(
        initial * transition.getcol(target_col).toarray().ravel()
    )
    if source_values.max(initial=0.0) <= 0:
        return ""
    source_col = int(np.argmax(source_values))
    return (
        f"C{communities[source_col].community_id} "
        f"{communities[source_col].label} ({source_values[source_col]:.6f})"
    )


def collect_tags_for_posts(post_tags_path: Path, target_ids: set[int]) -> dict[int, list[str]]:
    collected: dict[int, list[str]] = {}
    for post_id, tags in iter_post_tags(post_tags_path):
        if post_id in target_ids:
            collected[post_id] = list(dict.fromkeys(tags))
    return collected


def write_csv(path: Path, rows: Iterable[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(
    output_dir: Path,
    snapshot: Path,
    post_ids: np.ndarray,
    matrix: sparse.csr_matrix,
    communities: list[Community],
    transition: sparse.csr_matrix,
    users: dict[str, dict],
    tags_by_post: dict[int, list[str]],
    config: dict,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    overview_rows: list[dict] = []
    summary_users: dict[str, dict] = {}

    for user_name, result in users.items():
        user_dir = output_dir / user_name
        user_dir.mkdir(parents=True, exist_ok=True)
        history_rows = result["history_rows"]
        profiles = result["profiles"]
        recommendations = result["recommendations"]

        history_output: list[dict] = []
        for position, (row_number, recency) in enumerate(
            zip(history_rows, profiles["recency"]), start=1
        ):
            post_id = int(post_ids[row_number])
            post_row = matrix.getrow(row_number)
            top_local = sorted(
                zip(post_row.data, post_row.indices), reverse=True
            )[:4]
            history_output.append(
                {
                    "position_oldest_to_newest": position,
                    "post_id": post_id,
                    "recency_weight": round(float(recency), 6),
                    "community_contributions": " | ".join(
                        f"C{communities[int(col)].community_id} "
                        f"{communities[int(col)].label} ({float(weight):.4f})"
                        for weight, col in top_local
                    ),
                    "tags": " | ".join(tags_by_post.get(post_id, [])),
                }
            )
        write_csv(
            user_dir / "history.csv",
            history_output,
            [
                "position_oldest_to_newest",
                "post_id",
                "recency_weight",
                "community_contributions",
                "tags",
            ],
        )

        initial_rows: list[dict] = []
        for rank, col in enumerate(profiles["initial_cols"], start=1):
            initial_rows.append(
                {
                    "rank": rank,
                    "community_id": communities[int(col)].community_id,
                    "community_label": communities[int(col)].label,
                    "selected_profile_weight": round(
                        float(profiles["initial_selected"][col]), 8
                    ),
                    "full_profile_weight": round(
                        float(profiles["initial_full"][col]), 8
                    ),
                    "recommendation_quota": result["initial_realized_quotas"].get(
                        int(col), 0
                    ),
                    "community_size": communities[int(col)].size,
                    "post_count": communities[int(col)].post_count,
                }
            )
        write_csv(
            user_dir / "initial_communities.csv",
            initial_rows,
            [
                "rank",
                "community_id",
                "community_label",
                "selected_profile_weight",
                "full_profile_weight",
                "recommendation_quota",
                "community_size",
                "post_count",
            ],
        )

        expanded_rows: list[dict] = []
        for rank, col in enumerate(profiles["expanded_cols"], start=1):
            expanded_rows.append(
                {
                    "rank": rank,
                    "community_id": communities[int(col)].community_id,
                    "community_label": communities[int(col)].label,
                    "selected_profile_weight": round(
                        float(profiles["expanded_selected"][col]), 8
                    ),
                    "full_expansion_weight": round(
                        float(profiles["expanded_full"][col]), 8
                    ),
                    "recommendation_quota": result["expanded_realized_quotas"].get(
                        int(col), 0
                    ),
                    "strongest_initial_source": strongest_source_for_expansion(
                        int(col), profiles["initial_full"], transition, communities
                    ),
                    "community_size": communities[int(col)].size,
                    "post_count": communities[int(col)].post_count,
                }
            )
        write_csv(
            user_dir / "expanded_communities.csv",
            expanded_rows,
            [
                "rank",
                "community_id",
                "community_label",
                "selected_profile_weight",
                "full_expansion_weight",
                "recommendation_quota",
                "strongest_initial_source",
                "community_size",
                "post_count",
            ],
        )

        recommendation_rows: list[dict] = []
        channel_counts = Counter(channel for _, channel, _ in recommendations)
        assigned_counts = Counter(
            (channel, assigned_col)
            for _, channel, assigned_col in recommendations
        )
        for rank, (row_number, channel, assigned_col) in enumerate(
            recommendations, start=1
        ):
            post_id = int(post_ids[row_number])
            post_row = matrix.getrow(row_number)
            preference = (
                profiles["initial_selected"]
                if channel == "initial"
                else profiles["expanded_selected"]
            )
            record = {
                "rank": rank,
                "post_id": post_id,
                "recall_channel": channel,
                "assigned_community_id": communities[assigned_col].community_id,
                "assigned_community_label": communities[assigned_col].label,
                "channel_score": round(
                    float(
                        result["initial_scores"][row_number]
                        if channel == "initial"
                        else result["expanded_scores"][row_number]
                    ),
                    8,
                ),
                "initial_score": round(float(result["initial_scores"][row_number]), 8),
                "expanded_score": round(
                    float(result["expanded_scores"][row_number]), 8
                ),
                "matched_communities": community_contributions(
                    post_row, preference, communities
                ),
                "tags": " | ".join(tags_by_post.get(post_id, [])),
            }
            recommendation_rows.append(record)
            overview_rows.append({"user_type": user_name, **record})
        write_csv(
            user_dir / "recommendations.csv",
            recommendation_rows,
            [
                "rank",
                "post_id",
                "recall_channel",
                "assigned_community_id",
                "assigned_community_label",
                "channel_score",
                "initial_score",
                "expanded_score",
                "matched_communities",
                "tags",
            ],
        )
        summary_users[user_name] = {
            "description": result["description"],
            "history_post_ids": [int(post_ids[row]) for row in history_rows],
            "recommendation_count": len(recommendations),
            "initial_recommendation_count": channel_counts["initial"],
            "expanded_recommendation_count": channel_counts["expanded"],
            "initial_community_recommendation_counts": {
                f"C{communities[int(col)].community_id} {communities[int(col)].label}": (
                    assigned_counts[("initial", int(col))]
                )
                for col in profiles["initial_cols"]
            },
            "expanded_community_recommendation_counts": {
                f"C{communities[int(col)].community_id} {communities[int(col)].label}": (
                    assigned_counts[("expanded", int(col))]
                )
                for col in profiles["expanded_cols"]
            },
            "initial_communities": [
                f"C{communities[int(col)].community_id} {communities[int(col)].label}"
                for col in profiles["initial_cols"]
            ],
            "expanded_communities": [
                f"C{communities[int(col)].community_id} {communities[int(col)].label}"
                for col in profiles["expanded_cols"]
            ],
        }

    write_csv(
        output_dir / "all_recommendations.csv",
        overview_rows,
        [
            "user_type",
            "rank",
            "post_id",
            "recall_channel",
            "assigned_community_id",
            "assigned_community_label",
            "channel_score",
            "initial_score",
            "expanded_score",
            "matched_communities",
            "tags",
        ],
    )
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": str(snapshot),
        "config": config,
        "users": summary_users,
    }
    with (output_dir / "simulation_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    report_lines = [
        "# 用户推荐流程模拟",
        "",
        f"- 快照：`{snapshot}`",
        f"- 推荐硬配额：初始兴趣 {config['initial_quota']} 条，扩散兴趣 "
        f"{config['recommendation_count'] - config['initial_quota']} 条",
        f"- 初始社区保留：MMR 选择 {config['top_initial_communities']} 个",
        f"- 扩散社区保留：MMR 选择 {config['top_expanded_communities']} 个",
        f"- 社区 MMR λ：{config['community_mmr_lambda']}",
        f"- 单个社区最高份额：{config['max_community_share']:.0%}",
        "",
    ]
    for user_name, user_summary in summary_users.items():
        report_lines.extend(
            [
                f"## {user_name}",
                "",
                user_summary["description"],
                "",
                f"- 实际推荐：{user_summary['recommendation_count']} 条",
                f"- 初始兴趣通道：{user_summary['initial_recommendation_count']} 条",
                f"- 扩散兴趣通道：{user_summary['expanded_recommendation_count']} 条",
                "- 初始社区：" + "；".join(user_summary["initial_communities"]),
                "- 初始社区推荐数："
                + "；".join(
                    f"{name}={count}"
                    for name, count in user_summary[
                        "initial_community_recommendation_counts"
                    ].items()
                ),
                "- 扩散社区：" + "；".join(user_summary["expanded_communities"]),
                "- 扩散社区推荐数："
                + "；".join(
                    f"{name}={count}"
                    for name, count in user_summary[
                        "expanded_community_recommendation_counts"
                    ].items()
                ),
                "",
            ]
        )
    (output_dir / "README.md").write_text(
        "\n".join(report_lines), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path("/Users/zhu/Desktop/麦粒科技/推荐系统"),
    )
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--recommendation-count", type=int, default=30)
    parser.add_argument("--initial-quota", type=int, default=21)
    parser.add_argument("--top-initial-communities", type=int, default=6)
    parser.add_argument("--top-expanded-communities", type=int, default=6)
    parser.add_argument("--community-mmr-lambda", type=float, default=0.75)
    parser.add_argument("--mmr-candidate-pool", type=int, default=20)
    parser.add_argument("--community-quota-power", type=float, default=0.5)
    parser.add_argument("--max-community-share", type=float, default=0.30)
    parser.add_argument("--half-life-posts", type=float, default=5.0)
    parser.add_argument("--synonym-discount", type=float, default=0.3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0 < args.initial_quota < args.recommendation_count:
        raise ValueError("initial_quota 必须严格位于 0 和 recommendation_count 之间")

    snapshot = args.snapshot or find_latest_snapshot(args.project_dir)
    output_dir = args.output_dir or snapshot / "user_simulation_results"
    post_tags_path = snapshot / "post_tags.csv"
    print(f"使用快照：{snapshot}", flush=True)

    tag_to_community, communities, community_to_col = load_communities(snapshot)
    print(
        f"社区 {len(communities):,} 个；非孤立社区标签 {len(tag_to_community):,} 个",
        flush=True,
    )

    tag_df, total_posts = count_tag_document_frequency(post_tags_path)
    tag_quality = compute_tag_quality(tag_df, total_posts)
    post_ids, matrix = build_post_community_matrix(
        post_tags_path,
        tag_to_community,
        community_to_col,
        tag_quality,
        args.synonym_discount,
    )
    print(
        f"帖子-社区矩阵：{matrix.shape[0]:,}×{matrix.shape[1]:,}，"
        f"非零项 {matrix.nnz:,}",
        flush=True,
    )

    transition = load_transition_matrix(snapshot, communities, community_to_col)
    histories = simulate_histories(matrix, community_to_col, args.seed)
    matrix_csc = matrix.tocsc()

    users: dict[str, dict] = {}
    target_post_ids: set[int] = set()
    for user_name, history in histories.items():
        history_rows = history["history_rows"]
        profiles = build_user_profiles(
            matrix,
            transition,
            history_rows,
            args.half_life_posts,
            args.top_initial_communities,
            args.top_expanded_communities,
            args.community_mmr_lambda,
            args.mmr_candidate_pool,
        )
        initial_scores = np.asarray(matrix @ profiles["initial_selected"]).ravel()
        expanded_scores = np.asarray(matrix @ profiles["expanded_selected"]).ravel()
        initial_posts, initial_realized_quotas = select_posts_by_community(
            matrix_csc,
            initial_scores,
            profiles["initial_selected"],
            args.initial_quota,
            set(history_rows),
            args.community_quota_power,
            args.max_community_share,
        )
        expanded_posts, expanded_realized_quotas = select_posts_by_community(
            matrix_csc,
            expanded_scores,
            profiles["expanded_selected"],
            args.recommendation_count - args.initial_quota,
            set(history_rows) | {row for row, _ in initial_posts},
            args.community_quota_power,
            args.max_community_share,
        )
        recommendations = interleave_channels(initial_posts, expanded_posts)
        users[user_name] = {
            **history,
            "profiles": profiles,
            "initial_scores": initial_scores,
            "expanded_scores": expanded_scores,
            "initial_realized_quotas": initial_realized_quotas,
            "expanded_realized_quotas": expanded_realized_quotas,
            "recommendations": recommendations,
        }
        target_post_ids.update(int(post_ids[row]) for row in history_rows)
        target_post_ids.update(int(post_ids[row]) for row, _, _ in recommendations)
        counts = Counter(channel for _, channel, _ in recommendations)
        print(
            f"[3/4] {user_name}: 推荐 {len(recommendations)} 条，"
            f"初始 {counts['initial']}（{len(initial_realized_quotas)} 社区），"
            f"扩散 {counts['expanded']}（{len(expanded_realized_quotas)} 社区）",
            flush=True,
        )

    tags_by_post = collect_tags_for_posts(post_tags_path, target_post_ids)
    config = {
        "recommendation_count": args.recommendation_count,
        "initial_quota": args.initial_quota,
        "expanded_quota": args.recommendation_count - args.initial_quota,
        "top_initial_communities": args.top_initial_communities,
        "top_expanded_communities": args.top_expanded_communities,
        "community_mmr_lambda": args.community_mmr_lambda,
        "mmr_candidate_pool": args.mmr_candidate_pool,
        "community_quota_power": args.community_quota_power,
        "max_community_share": args.max_community_share,
        "half_life_posts": args.half_life_posts,
        "synonym_discount": args.synonym_discount,
        "tag_quality": "sqrt(log(1 + N/(df+5))) * sqrt(df/(df+5))",
        "transition_graph": "Top-20 directed weighted graph; one-hop only",
        "dead_end_policy": "self-loop",
    }
    write_outputs(
        output_dir,
        snapshot,
        post_ids,
        matrix,
        communities,
        transition,
        users,
        tags_by_post,
        config,
    )
    print(f"[4/4] 完成，结果目录：{output_dir}", flush=True)


if __name__ == "__main__":
    main()
