from __future__ import annotations

from typing import Any

from simcore.sampler import Sample

from .models import ExportError


def validate_split_args(*, shard_size: int | None, num_shards: int | None) -> None:
    if shard_size is not None and num_shards is not None:
        raise ExportError("shard-size and num-shards are mutually exclusive")
    if shard_size is None and num_shards is None:
        raise ExportError("one of shard-size or num-shards is required")
    if shard_size is not None and shard_size <= 0:
        raise ExportError("shard-size must be greater than 0")
    if num_shards is not None and num_shards <= 0:
        raise ExportError("num-shards must be greater than 0")


def collect_samples(sampler: Any) -> list[Sample]:
    samples: list[Sample] = []
    index = 1
    while True:
        sample = sampler.next()
        if sample is None:
            return samples
        sample_id = str(sample.id) if sample.id is not None else str(index)
        samples.append(Sample(id=sample_id, params=sample.params, metadata=sample.metadata))
        index += 1


def split_samples(
    samples: list[Sample],
    *,
    shard_size: int | None,
    num_shards: int | None,
) -> list[list[Sample]]:
    if shard_size is not None:
        return [samples[index : index + shard_size] for index in range(0, len(samples), shard_size)]

    assert num_shards is not None
    if not samples:
        return []
    base_size, remainder = divmod(len(samples), num_shards)
    shards: list[list[Sample]] = []
    start = 0
    for index in range(num_shards):
        current_size = base_size + (1 if index < remainder else 0)
        if current_size == 0:
            break
        end = start + current_size
        shards.append(samples[start:end])
        start = end
    return shards


def sample_to_dict(sample: Sample) -> dict[str, Any]:
    return {
        "id": sample.id,
        "params": sample.sim_params,
    }

