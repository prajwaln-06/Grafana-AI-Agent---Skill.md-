"""
Minimal stdlib HTTP client for the incident controller. Deliberately uses
only urllib so that exporter.py doesn't need a new dependency just to poll
for active incidents.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class ControllerUnavailable(Exception):
    pass


class ControllerClient:
    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path):
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise ControllerUnavailable(str(e)) from e

    def _post(self, path, payload):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise ValueError(f"controller rejected request: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise ControllerUnavailable(str(e)) from e

    def health(self):
        return self._get("/health")

    def active(self, node: str | None = None):
        path = "/scenarios/active"
        if node:
            path += f"?node={urllib.parse.quote(node)}"
        return self._get(path)["active"]

    def active_with_epoch(self, node: str | None = None):
        """Like active(), but also returns reset_epoch so callers that
        schedule future work (logsim) can detect a /scenarios/reset and
        drop anything they'd already queued."""
        path = "/scenarios/active"
        if node:
            path += f"?node={urllib.parse.quote(node)}"
        data = self._get(path)
        return data["active"], data.get("reset_epoch", 0)

    def reset(self):
        return self._post("/scenarios/reset", {})

    def trigger(self, scenario_id, node, gpu=None, mount=None,
                start_after=0.0, duration=None):
        payload = {
            "scenario_id": scenario_id,
            "node": node,
            "gpu": gpu,
            "mount": mount,
            "start_after": start_after,
            "duration": duration,
        }
        return self._post("/scenarios/trigger", payload)
