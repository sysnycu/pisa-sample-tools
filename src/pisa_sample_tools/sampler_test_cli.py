from __future__ import annotations

from pisa_sample_tools.sampler_preview.cli import build_parser, main, sampler_config_from_args

_sampler_config_from_args = sampler_config_from_args

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
