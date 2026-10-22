"""The shared source registry (data/registry.yaml): each jurisdiction's government domains and official newsrooms,
and the credible news outlets whose articles are read from search results."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from .store import DATA

REGISTRY = DATA / "registry.yaml"


class Registry:
    def __init__(self, data: dict):
        self.jurisdictions: dict[str, dict] = data.get("jurisdictions", {})
        self.credible = [d.lower() for d in data.get("credible_news", [])]

    @classmethod
    def load(cls, path: Path = REGISTRY) -> "Registry":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def domains(self, code: str) -> list[str]:
        return list((self.jurisdictions.get(code) or {}).get("domains", []))

    def newsrooms(self, code: str) -> list[dict]:
        return list((self.jurisdictions.get(code) or {}).get("newsrooms", []))

    @staticmethod
    def _host(url: str) -> str:
        return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")

    def jurisdiction_of(self, url: str) -> str | None:
        """The jurisdiction whose government domain serves this URL (cm.bihar.gov.in → BR)."""
        host = self._host(url)
        for code, j in self.jurisdictions.items():
            if any(host == d or host.endswith("." + d) for d in j.get("domains", [])):
                return code
        return None

    def is_credible(self, url: str) -> bool:
        host = self._host(url)
        return any(host == d or host.endswith("." + d) for d in self.credible)
