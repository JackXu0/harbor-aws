"""Process-wide types for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClusterInfo:
    """Operational metadata fetched from the control pod's ``/info`` endpoint."""

    namespace: str
    account_id: str
    k8s_service_account: str
    dockerhub_cache_enabled: bool


@dataclass(frozen=True)
class TrialOptions:
    """Per-trial overrides set by the adapter caller (kwargs to ``AWSEnvironment``)."""

    pod_timeout_sec: int = 14400
    use_bedrock: bool = False
