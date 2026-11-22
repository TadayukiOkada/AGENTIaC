// Application aliases for the generated API DTO contract.
export type {
  SessionDTO as Session, HardwareDTO as Hardware, NodeDTO as NodeRow,
  ModelTargetDTO as ModelRow, AgentCapabilitiesDTO as AgentCapability,
  AgentDTO as AgentRow, ConfigurationResourceDTO as ConfigurationResource,
  OverviewDTO as Overview, JobDTO as Job, JobAcceptedDTO as JobAccepted,
  JobListDTO as JobList, EventsDTO as Events, LogChunkDTO as LogChunk,
  NodeListDTO as NodeList, ModelListDTO as ModelList, AgentListDTO as AgentList,
  SnapshotDTO as Snapshot, RelatedDTO as Related, RelationDTO as Relation,
  EngineDTO as EngineRow, EngineListDTO as EngineList,
  DeploymentsDTO as Deployments, DashboardDeploymentDTO as DashboardDeployment,
} from './generated/api'
import type { ConfigurationResourcesDTO } from './generated/api'
export type ConfigurationResources = ConfigurationResourcesDTO
export type ConfigReport = Pick<ConfigurationResourcesDTO, 'resources' | 'route' | 'references' | 'target_names'>
export type Event = import('./generated/api').EventDTO
