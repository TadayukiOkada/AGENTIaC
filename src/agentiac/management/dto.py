"""Typed read DTOs for the management API.

These are allowlist shapes: they do not carry raw credential files, conversation text,
arbitrary journald or the whole inventory file. Unknown values stay `None`; they are never
turned into zero or offline.
"""
import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, ConfigDict

Permission = Literal['viewer', 'operator', 'admin']


class ErrorDTO(BaseModel):
    code: str
    message: str
    request_id: str
    retryable: bool = False
    details: list[str] = Field(default_factory=list)


class SessionDTO(BaseModel):
    actor: str
    permission: Permission
    csrf_token: str
    created_at: str
    expires_at: str


class SessionLoginDTO(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    password: str = Field(min_length=1, max_length=256)


class HardwareDTO(BaseModel):
    gpu: Optional[str] = None
    gpu_count: Optional[int] = None
    gpu_vram_mib: Optional[int] = None
    driver: Optional[str] = None
    memory_total_gib: Optional[float] = None
    hw_fp: Optional[str] = None
    collected_at: Optional[str] = None
    age_s: Optional[int] = None
    stale: Optional[bool] = None
    unknowns: list[str] = Field(default_factory=list)


class MinimalProbeDTO(BaseModel):
    system: str
    release: str
    machine: str
    cpu_count: Optional[int] = None
    memory_total_bytes: Optional[int] = None
    memory_available_bytes: Optional[int] = None
    source: Literal['linux-proc-v1']
    collected_at: str


class ProbeSnapshotDTO(BaseModel):
    data: MinimalProbeDTO
    job_id: str
    attempt: int
    recorded_at: str
    quality: Literal['fresh', 'stale']


class NodeDTO(BaseModel):
    id: str
    host: str
    user: Optional[str] = None
    port: Optional[int] = None
    node_class: Optional[str] = None
    borrowed: bool = False
    default_profile: Optional[str] = None
    spark_tensorfold: Optional[bool] = None
    tensorfold_backend: Optional[str] = None   # the manifest a spark_tensorfold node serves (backend_names)
    strata_default_backend: Optional[str] = None
    runtime_image: Optional[str] = None
    hardware: Optional[HardwareDTO] = None
    probe: Optional[ProbeSnapshotDTO] = None


class SnapshotDTO(BaseModel):
    """When the cached resource list was read from the declaration files.

    Age fields inside the rows (hardware/state `age_s`) are as of `generated_at`. `stale` is set
    when the latest rebuild failed and the previous snapshot is served; `error` names the class.
    """
    generated_at: str
    age_s: float
    ttl_s: int
    stale: bool = False
    error: Optional[str] = None


class NodeListDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    snapshot: Optional[SnapshotDTO] = None
    next_cursor: Optional[str] = None
    nodes: list[NodeDTO]


class ModelTargetDTO(BaseModel):
    target: str
    name: Optional[str] = None  # node-first display name; `target` stays the ID (naming migration stage 1)
    node: str
    current: bool
    declared: dict
    identity: dict
    state: dict
    evidence: dict


class ModelListDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    current_name: Optional[str] = None
    snapshot: Optional[SnapshotDTO] = None
    current: str
    next_cursor: Optional[str] = None
    targets: list[ModelTargetDTO]


class AgentCapabilitiesDTO(BaseModel):
    feature: str
    value: Literal['native', 'emulated', 'unverified', 'unsupported']
    reason: str
    version: Optional[str] = None
    probed_at: Optional[float] = None


class AgentDTO(BaseModel):
    id: str
    version: Optional[str] = None
    declared_version: Optional[str] = None
    install: Optional[str] = None
    entrypoint: Optional[str] = None
    endpoint: Optional[str] = None
    gateway_only: Optional[bool] = None
    allowed_paths: list[str] = Field(default_factory=list)
    pin: Optional[dict] = None
    capabilities: dict[str, AgentCapabilitiesDTO] = Field(default_factory=dict)


class AgentListDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    snapshot: Optional[SnapshotDTO] = None
    next_cursor: Optional[str] = None
    agents: list[AgentDTO]


class ConfigurationResourceDTO(BaseModel):
    id: str
    kind: str
    declared: dict


class ConfigurationResourcesDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    snapshot: Optional[SnapshotDTO] = None
    next_cursor: Optional[str] = None
    resources: list[ConfigurationResourceDTO]
    route: dict = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)
    target_names: dict[str, str] = Field(default_factory=dict)  # route key -> node-first display name


class ConfigurationHistoryDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    next_cursor: Optional[str] = None
    items: list[dict] = Field(default_factory=list)
    reason: Optional[str] = None


class LifecycleIssueDTO(BaseModel):
    code: str
    severity: Literal['error', 'warning']
    backend: Optional[str] = None
    node: Optional[str] = None
    source_resource_id: str
    message: str


class LifecycleSourceDTO(BaseModel):
    resource_id: str
    state: Optional[str] = None
    handle: Optional[str] = None


class LifecycleAssociationDTO(BaseModel):
    node: Optional[str] = None
    backend: str
    role: Literal['default', 'rollback', 'evaluation', 'quarantined', 'retired', 'declared_only']
    form: Optional[Literal['full', 'thin']] = None
    retirement_intent: Literal['candidate', 'declined', 'unspecified']
    reason: Optional[str] = None
    source: LifecycleSourceDTO


class LifecycleReferenceDTO(BaseModel):
    backend: str
    node: Optional[str] = None
    kind: Literal['target', 'claim_default', 'component', 'current']
    id: str
    source_resource_id: str


class LifecycleCatalogDTO(BaseModel):
    backend: str
    status: Literal['declared', 'archived']
    reason: Optional[str] = None
    evidence: Optional[str] = None


class EngineLifecycleDTO(BaseModel):
    coverage: Literal['complete', 'partial', 'unknown']
    catalog_states: list[LifecycleCatalogDTO]
    roles: list[Literal['default', 'rollback', 'evaluation', 'quarantined', 'retired', 'declared_only']]
    nodes: list[str]
    history_only: bool
    associations: list[LifecycleAssociationDTO]
    references: list[LifecycleReferenceDTO]
    issues: list[LifecycleIssueDTO]


class EngineDTO(BaseModel):
    """One engine source release and what declares or uses it. `declared` is false when the backend
    manifest names no engine repository; its unknown fields stay None."""
    id: str
    name: Optional[str] = None
    repository: Optional[str] = None
    version: Optional[str] = None
    revision: Optional[str] = None
    ggml_revisions: list[str] = Field(default_factory=list)
    backends: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
    current: bool = False
    declared: bool = True
    lifecycle: Optional[EngineLifecycleDTO] = None


class EngineListDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    snapshot: Optional[SnapshotDTO] = None
    next_cursor: Optional[str] = None
    engines: list[EngineDTO]
    lifecycle_issues: list[LifecycleIssueDTO] = Field(default_factory=list)
    target_names: dict[str, str] = Field(default_factory=dict)  # route key -> node-first display name


class ControlDeployDTO(BaseModel):
    revision: Optional[str] = None
    deployed_at: Optional[str] = None
    dirty: Optional[bool] = None
    head: Optional[str] = None
    up_to_date: Optional[bool] = None


class RouteChangeDTO(BaseModel):
    time: Optional[str] = None
    from_target: Optional[str] = None
    to_target: str


class RouteStateDTO(BaseModel):
    current: Optional[str] = None
    declared_default: Optional[str] = None
    changes: list[RouteChangeDTO] = Field(default_factory=list)


class DashboardDeploymentDTO(BaseModel):
    """One dashboard plan/apply record. No unit text, plan contents or TLS material; a release is named
    by its directory name."""
    id: str
    action: Optional[str] = None
    status: Literal['current', 'superseded', 'failed', 'emergency stop']
    applied_at: Optional[str] = None
    plan_hash: Optional[str] = None
    release: Optional[str] = None
    release_id: Optional[str] = None
    revision: Optional[str] = None
    previous_release: Optional[str] = None
    port: Optional[int] = None
    transport: Optional[Literal['ssh-tunnel', 'tls']] = None
    probe_targets: list[str] = Field(default_factory=list)
    enabled_at_login: bool = False


class DeploymentsDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    control: ControlDeployDTO
    route: RouteStateDTO
    dashboard_current: Optional[str] = None
    dashboard: list[DashboardDeploymentDTO] = Field(default_factory=list)
    target_names: dict[str, str] = Field(default_factory=dict)  # route key -> node-first display name


class RelationDTO(BaseModel):
    kind: Literal['nodes', 'models', 'engines', 'agents', 'configuration', 'jobs']
    id: str
    relation: str
    exists: bool


class RelatedDTO(BaseModel):
    """Resources related to one declared resource, derived from the cached snapshots."""
    schema_version: int = 1
    as_of: str
    kind: Literal['nodes', 'models', 'engines', 'agents', 'configuration']
    id: str
    relations: list[RelationDTO]
    snapshots: dict[str, SnapshotDTO] = Field(default_factory=dict)
    target_names: dict[str, str] = Field(default_factory=dict)  # route key -> node-first display name


class OperationDTO(BaseModel):
    id: str
    phase: str
    permission: Permission
    readonly: bool
    implemented: bool
    reason: str


class CapabilitiesDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    operations: list[OperationDTO]


class OverviewDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    operations: list[OperationDTO]
    freshness: dict[str, str]


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
