"""
The firm list the sweep runs over = hand-written firms.yaml + auto-discovered
firms, deduped by normalized name.

Hand-written entries always win: they carry regions, a category I chose, and
verified ATS hints. A discovered entry only fills a gap.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from .discover import is_known, name_key, to_registry

log = logging.getLogger("radar.registry")


def load(firms_path: Path, data_dir: Path, *, limit: int | None = None,
         include_discovered: bool = True) -> list[dict]:
    doc = yaml.safe_load(firms_path.read_text()) or {}
    base = doc.get("firms", [])
    for f in base:
        f.setdefault("discovered", False)

    merged = list(base)
    if include_discovered:
        cand_path = data_dir / "candidates.json"
        if cand_path.exists():
            try:
                cands = json.loads(cand_path.read_text())
            except json.JSONDecodeError:
                cands = {}
            known = {name_key(f["name"]) for f in base}
            extra = [f for f in to_registry(cands)
                     if not is_known(name_key(f["name"]), known)]
            # dedupe discovered against itself too
            seen: set[str] = set()
            for f in extra:
                k = name_key(f["name"])
                if is_known(k, seen):
                    continue
                seen.add(k)
                merged.append(f)
            if extra:
                log.info("registry: %d curated + %d discovered = %d firms",
                         len(base), len(merged) - len(base), len(merged))

    return merged[:limit] if limit else merged


def known_keys(firms_path: Path) -> set[str]:
    doc = yaml.safe_load(firms_path.read_text()) or {}
    return {name_key(f["name"]) for f in doc.get("firms", [])}
