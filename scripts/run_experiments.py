"""Run the controlled experiments required by REPORT_TEMPLATE.md.

All variants are created in memory.  The production configuration and client
implementation are never rewritten for an experiment.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gameday_lab.cache import CachingClient, SemanticCache
from gameday_lab.client import ResilientClient
from gameday_lab.sim.config import LabConfig, load_config
from gameday_lab.sim.engine import run_scenario


class UnfilteredClient(ResilientClient):
    """Experiment-only client that deliberately ignores tool capability."""

    def ladder(self, task: str) -> list[str]:
        return list(self.order)


def with_client(cfg: LabConfig, updates: dict[str, Any]) -> LabConfig:
    settings = copy.deepcopy(cfg.client)
    for key, value in updates.items():
        if key == "breaker":
            settings["breaker"].update(value)
        else:
            settings[key] = value
    return replace(cfg, client=settings)


def metrics(cfg: LabConfig, scenario: str, seeds: list[int], *, mode: str = "resilient",
            cache_updates: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    runs = []
    for seed in seeds:
        if mode == "unfiltered":
            client = UnfilteredClient(cfg.provider_infos(), cfg.fallback_order, cfg.client, seed=seed)
        elif mode == "cached":
            cache_cfg = copy.deepcopy(cfg.cache)
            cache_cfg.update(cache_updates or {})
            inner = ResilientClient(cfg.provider_infos(), cfg.fallback_order, cfg.client, seed=seed)
            client = CachingClient(inner, SemanticCache(**cache_cfg))
        else:
            client = ResilientClient(cfg.provider_infos(), cfg.fallback_order, cfg.client, seed=seed)
        runs.append({"seed": seed, **run_scenario(cfg, scenario, client, seed).metrics()})
    return runs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default="25,26,27")
    parser.add_argument("--out", default="reports/experiments.json")
    args = parser.parse_args()
    seeds = [int(value) for value in args.seeds.split(",")]

    cfg = load_config("configs/gameday.yaml", "configs/scenarios.yaml")
    cache_cfg = load_config("configs/gameday.yaml", "configs/scenarios_cache.yaml")
    breaker_disabled = {"min_calls": 1_000_000_000}
    budget_on = with_client(cfg, {"max_attempts_per_provider": 5, "breaker": breaker_disabled})
    budget_off = with_client(cfg, {
        "max_attempts_per_provider": 5,
        "retry_budget_ratio": 100,
        "retry_budget_max_tokens": 1.0e9,
        "retry_budget_min_per_s": 1.0e6,
        "breaker": breaker_disabled,
    })
    timeout_30 = with_client(cfg, {"attempt_timeout_s": 30})

    output: dict[str, Any] = {
        "seeds": seeds,
        "retry_budget": {},
        "timeout_30s": {},
        "tool_filter_disabled": {},
        "cache_threshold": {},
        "cache_ttl": {},
        "cache_stale_disabled": {},
    }
    for scenario in ("G2_overload_storm", "G3_brownout"):
        output["retry_budget"][scenario] = {
            "enabled": metrics(budget_on, scenario, seeds),
            "disabled": metrics(budget_off, scenario, seeds),
        }
    for scenario in ("G4_slow_region", "G6_provider_outage"):
        output["timeout_30s"][scenario] = metrics(timeout_30, scenario, seeds)
    output["tool_filter_disabled"]["G6_provider_outage"] = metrics(
        cfg, "G6_provider_outage", seeds, mode="unfiltered"
    )
    for threshold in (0.80, cfg.cache["similarity_threshold"], 0.97):
        output["cache_threshold"][str(threshold)] = metrics(
            cache_cfg, "C1_faq_outage", seeds, mode="cached",
            cache_updates={"similarity_threshold": threshold},
        )
    for ttl in (30, 60, 300):
        output["cache_ttl"][str(ttl)] = metrics(
            cache_cfg, "C1_faq_outage", seeds, mode="cached", cache_updates={"ttl_s": ttl}
        )
    output["cache_stale_disabled"]["0"] = metrics(
        cache_cfg, "C1_faq_outage", seeds, mode="cached", cache_updates={"max_stale_s": 0}
    )

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
