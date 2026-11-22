"""Management API: localhost-only, authenticated, P2b/P2c/P3.

Every GET requires an opaque server-side session cookie. No credential is put in a URL.
POST /session is rate-limited and origin/Host allowlisted. DELETE /session requires the
CSRF token. Probe jobs are accepted explicitly; configure, apply and destructive
operations remain registered and unimplemented. The built dashboard assets are served
from the same localhost origin.
"""
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .dto import (ErrorDTO, SessionDTO, SessionLoginDTO, CapabilitiesDTO, OverviewDTO, now_iso,
                  NodeDTO, NodeListDTO, ProbeSnapshotDTO, ModelTargetDTO, ModelListDTO, AgentDTO, AgentListDTO,
                  ConfigurationResourceDTO, ConfigurationResourcesDTO, ConfigurationHistoryDTO, RelatedDTO,
                  EngineDTO, EngineListDTO, DeploymentsDTO, DashboardDeploymentDTO)
from .errors import ApiError
from .operations import OPERATIONS, operations_for
from .cache import SnapshotCache, SnapshotUnavailable, page
from .reads import SNAPSHOTS
from .relations import related as related_resources
from .engines import build_engine_snapshot
from . import deployments as deployment_records
from agentiac.operations.dto import JobRequestDTO, JobAcceptedDTO, JobDTO, JobListDTO, EventsDTO, LogChunkDTO
from agentiac.operations import ledger as ledger_module
from agentiac.operations.ledger import Ledger
from .session import SessionStore

from .paths import ROOT, SOURCE_ROOT, SESSION_PATH, DEPLOYMENT_HISTORY, require_ready
from .lifecycle import StateLock


@asynccontextmanager
async def lifespan(application):
    with StateLock(SESSION_PATH.parent):
        require_ready(SESSION_PATH.parent)
        from .snapshots import validate_startup
        validate_startup(SESSION_PATH.parent)
        yield

app = FastAPI(title='agentiac management API', version='0.1', lifespan=lifespan,
              responses={code: {'model': ErrorDTO} for code in (400, 401, 403, 404, 409, 410, 413, 415, 422, 429, 503)})


async def security_headers(request, call_next):
    host = request.headers.get('host', '')
    if not LOCAL_HOST.fullmatch(host):
        response = await api_error_handler(request, ApiError(400, 'host_not_allowed', 'localhost Host is required'))
    elif request.method in {'POST', 'PATCH', 'PUT', 'DELETE'} and not origin_allowed(request):
        response = await api_error_handler(request, ApiError(403, 'origin_not_allowed', 'same origin is required'))
    elif request.method in {'POST', 'PATCH', 'PUT'}:
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            response = await api_error_handler(request, ApiError(415, 'content_type', 'JSON content type is required'))
        else:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    break
            if len(body) > 16384:
                response = await api_error_handler(request, ApiError(413, 'body_size', 'request body exceeds 16 KiB'))
            else:
                request._body = bytes(body)
                response = await call_next(request)
    else:
        response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'self'")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    return response


app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)

LOCAL_HOST = re.compile(r'(127\.0\.0\.1|localhost)(?::\d{1,5})?')


# State files whose directory, permissions and schema this process has already prepared.
# Later requests open them without DDL/chmod and refuse a missing file instead of recreating it.
_PREPARED = set()
_PREPARED_LOCK = threading.Lock()


def _prepared(key, factory):
    with _PREPARED_LOCK:
        fresh = key not in _PREPARED
        store = factory(create=fresh)
        _PREPARED.add(key)
    return store


def operation_ledger():
    ledger = _prepared(('ledger', str(ledger_module.DB_PATH), str(ROOT)),
                       lambda create: Ledger(ROOT, create=create))
    try:
        yield ledger
    finally:
        ledger.close()


def session_store():
    return _prepared(('session', str(SESSION_PATH)), lambda create: SessionStore(SESSION_PATH, create=create))


def origin_allowed(request: Request):
    origin = request.headers.get('origin')
    host = request.headers.get('host')
    return bool(origin and host and LOCAL_HOST.fullmatch(host)
                and origin == f'{request.url.scheme}://{host}')


def require_session(request: Request):
    sid = request.cookies.get('management_session')
    if not sid:
        raise ApiError(401, 'unauthorized', 'no server-side session cookie', retryable=False)
    session = session_store().read(sid)
    if session is None:
        raise ApiError(401, 'unauthorized', 'session is missing or expired', retryable=False)
    return session


def check_permission(session, required):
    rank = {'viewer': 0, 'operator': 1, 'admin': 2}
    if rank[session['permission']] < rank[required]:
        raise ApiError(403, 'permission', f'{required} permission is required', retryable=False)


def require_csrf(request: Request, session):
    token = request.headers.get('x-csrf-token')
    if token != session['csrf_token']:
        raise ApiError(403, 'csrf', 'CSRF token does not match the session', retryable=False)
    if not origin_allowed(request):
        raise ApiError(403, 'origin_not_allowed', 'only localhost origin is allowed', retryable=False)


@app.exception_handler(ApiError)
async def api_error_handler(request, error):
    return JSONResponse(status_code=error.status,
                        content=ErrorDTO(code=error.code, message=error.message,
                                   request_id=uuid4().hex, retryable=error.retryable,
                                   details=error.details).model_dump())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, error):
    # Pydantic errors can contain the complete submitted password/body; never echo them.
    return await api_error_handler(request, ApiError(422, 'schema_error', 'request validation failed'))


@app.get('/api/v1/capabilities', response_model=CapabilitiesDTO)
def capabilities(request: Request, session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    return CapabilitiesDTO(schema_version=1, as_of=now_iso(), operations=operations_for(ledger))


# Resource lists and details read per-process snapshots of the declaration files (cache.py).
SNAPSHOT_CACHE = SnapshotCache()


def snapshot(kind):
    build, row_id = ((build_engine_snapshot, lambda row: row['id']) if kind == 'engines'
                     else SNAPSHOTS[kind])
    try:
        return SNAPSHOT_CACHE.get(kind, str(ROOT), lambda: build(ROOT), row_id)
    except SnapshotUnavailable:
        raise ApiError(503, 'snapshot_unavailable', f'{kind} declarations could not be read; see the API journal',
                       retryable=True) from None


def listing(kind, limit, cursor):
    current = snapshot(kind)
    try:
        rows, next_cursor = page(current, limit, cursor)
    except ValueError as error:
        raise ApiError(422, 'schema_error', str(error), retryable=False) from None
    return current, rows, {'schema_version': 1, 'as_of': now_iso(), 'next_cursor': next_cursor,
                           'snapshot': SNAPSHOT_CACHE.describe(current)}


def target_names():
    """Route key -> node-first display name, for responses that mention route targets. Retired keys
    (naming stage 2) map to their new target's name, so saved logs and history read in today's names."""
    current = snapshot('models').extra['names']
    retired = snapshot('models').extra.get('retired', {})
    return {**{old: current.get(new, new) for old, new in retired.items()}, **current}


def with_probe(row, ledger):
    # model_copy does not validate; build the DTO so the ledger record is checked as before.
    probe = ledger.probe_snapshot(row.id)
    return row.model_copy(update={'probe': ProbeSnapshotDTO.model_validate(probe) if probe else None})


@app.get('/api/v1/overview', response_model=OverviewDTO)
def overview(request: Request, session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    declared = SNAPSHOT_CACHE.freshness(str(ROOT)) or {'declared': 'no snapshot read yet'}
    return OverviewDTO(schema_version=1, as_of=now_iso(), operations=operations_for(ledger),
                       freshness={**declared, 'hardware': 'artifact freshness unknown', 'status': 'unknown'})


@app.get('/api/v1/nodes', response_model=NodeListDTO)
def nodes(request: Request, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None, ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    _, rows, body = listing('nodes', limit, cursor)
    return {**body, 'nodes': [with_probe(row, ledger) for row in rows]}


@app.get('/api/v1/nodes/{node_id}', response_model=NodeDTO)
def node(request: Request, node_id: str, session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    row = snapshot('nodes').by_id(node_id)
    if row is None:
        raise ApiError(404, 'unknown_node', 'node is not declared in inventory', retryable=False)
    return with_probe(row, ledger)


@app.get('/api/v1/models', response_model=ModelListDTO)
def models(request: Request, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None):
    check_permission(session, 'viewer')
    current, rows, body = listing('models', limit, cursor)
    return {**body, 'current': current.extra['current'], 'current_name': current.extra['names'].get(current.extra['current']),
            'targets': rows}


@app.get('/api/v1/models/{target}', response_model=ModelTargetDTO)
def model(request: Request, target: str, session=Depends(require_session)):
    check_permission(session, 'viewer')
    models = snapshot('models')
    row = models.by_id(models.extra.get('retired', {}).get(target, target))  # a retired key still finds its target
    if row is None:
        raise ApiError(404, 'unknown_model', 'model target is not in the catalogue', retryable=False)
    return row


@app.get('/api/v1/engines', response_model=EngineListDTO)
def engines(request: Request, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None):
    check_permission(session, 'viewer')
    current, rows, body = listing('engines', limit, cursor)
    return {**body, 'engines': rows, 'lifecycle_issues': current.extra.get('lifecycle_issues', []),
            'target_names': current.extra.get('names', {})}


@app.get('/api/v1/engines/{engine_id}', response_model=EngineDTO)
def engine(request: Request, engine_id: str, session=Depends(require_session)):
    check_permission(session, 'viewer')
    row = snapshot('engines').by_id(engine_id)
    if row is None:
        raise ApiError(404, 'unknown_engine', 'no declared backend pins this engine release', retryable=False)
    return row


@app.get('/api/v1/deployments', response_model=DeploymentsDTO)
def deployments(request: Request, session=Depends(require_session)):
    """Control deploy, route and dashboard release history from local files (bounded; no command runs)."""
    check_permission(session, 'viewer')
    history = deployment_records.dashboard(DEPLOYMENT_HISTORY)
    return {'schema_version': 1, 'as_of': now_iso(), 'control': deployment_records.control(ROOT),
            'route': deployment_records.route(ROOT, snapshot('models').extra['current']),
            'dashboard_current': history['current'], 'dashboard': history['entries'], 'target_names': target_names()}


@app.get('/api/v1/deployments/{deployment_id}', response_model=DashboardDeploymentDTO)
def deployment(request: Request, deployment_id: str, session=Depends(require_session)):
    check_permission(session, 'viewer')
    for entry in deployment_records.dashboard(DEPLOYMENT_HISTORY)['entries']:
        if entry['id'] == deployment_id:
            return entry
    raise ApiError(404, 'unknown_deployment', 'no dashboard deployment record has this ID', retryable=False)


@app.get('/api/v1/agents', response_model=AgentListDTO)
def agents(request: Request, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None):
    check_permission(session, 'viewer')
    _, rows, body = listing('agents', limit, cursor)
    return {**body, 'agents': rows}


@app.get('/api/v1/agents/{agent_id}', response_model=AgentDTO)
def agent(request: Request, agent_id: str, session=Depends(require_session)):
    check_permission(session, 'viewer')
    row = snapshot('agents').by_id(agent_id)
    if row is None:
        raise ApiError(404, 'unknown_agent', 'agent is not declared', retryable=False)
    return row


@app.get('/api/v1/configuration/resources', response_model=ConfigurationResourcesDTO)
def resources(request: Request, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None):
    check_permission(session, 'viewer')
    current, rows, body = listing('configuration', limit, cursor)
    return {**body, 'resources': rows, 'route': current.extra['route'], 'references': current.extra['references'],
            'target_names': target_names()}


@app.get('/api/v1/configuration/resources/{resource_id:path}', response_model=ConfigurationResourceDTO)
def resource(request: Request, resource_id: str, session=Depends(require_session)):
    check_permission(session, 'viewer')
    row = snapshot('configuration').by_id(resource_id)
    if row is None:
        raise ApiError(404, 'unknown_resource', 'configuration resource is not declared', retryable=False)
    return row


RELATED_JOBS = 10


@app.get('/api/v1/related/{kind}/{resource_id:path}', response_model=RelatedDTO)
def related(request: Request, kind: Literal['nodes', 'models', 'engines', 'agents', 'configuration'], resource_id: str,
            session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    snapshots = {name: snapshot(name) for name in SNAPSHOTS}
    snapshots['engines'] = snapshot('engines')
    if kind == 'models':  # a retired key (naming stage 2) still names its target
        resource_id = snapshots['models'].extra.get('retired', {}).get(resource_id, resource_id)
    jobs = ledger.recent_for_target(resource_id, RELATED_JOBS) if kind == 'nodes' else ()
    relations = related_resources(kind, resource_id, snapshots, jobs)
    if relations is None:
        raise ApiError(404, 'unknown_resource', f'{kind} has no resource with this ID', retryable=False)
    return {'schema_version': 1, 'as_of': now_iso(), 'kind': kind, 'id': resource_id, 'relations': relations,
            'snapshots': {name: SNAPSHOT_CACHE.describe(value) for name, value in snapshots.items()},
            'target_names': target_names()}


@app.get('/api/v1/configuration/history', response_model=ConfigurationHistoryDTO)
def configuration_history(request: Request, session=Depends(require_session)):
    check_permission(session, 'viewer')
    return {'schema_version': 1, 'as_of': now_iso(), 'next_cursor': None,
            'items': [], 'reason': 'configuration history ledger is P2c'}


@app.post('/api/v1/jobs', response_model=JobAcceptedDTO, status_code=202)
def accept_job(request: Request, body: JobRequestDTO, session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'operator')
    require_csrf(request, session)
    accepted = ledger.accept(body, session['actor'], session['permission'],
                             timeout_s=body.timeout_s, require_worker=True)
    response = JSONResponse(JobAcceptedDTO(**accepted).model_dump(), status_code=202)
    response.headers['Location'] = accepted['location']
    return response


@app.get('/api/v1/jobs', response_model=JobListDTO)
def jobs(request: Request, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None,
         order: Literal['asc', 'desc'] = 'asc', ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    report = ledger.list(cursor, limit, newest_first=order == 'desc')
    return JobListDTO(**report)


@app.get('/api/v1/jobs/{job_id}', response_model=JobDTO)
def job(request: Request, job_id: str, session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    row = ledger.get(job_id)
    if row is None:
        raise ApiError(404, 'unknown_job', 'job is not in the ledger', retryable=False)
    return JobDTO(**row)


@app.post('/api/v1/jobs/{job_id}/cancel')
def cancel_job(request: Request, job_id: str, session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'operator')
    require_csrf(request, session)
    report = ledger.cancel(job_id, session['permission'])
    return {'schema_version': 1, 'as_of': now_iso(), **report}


@app.post('/api/v1/jobs/{job_id}/retry')
def retry_job(request: Request, job_id: str, session=Depends(require_session), ledger=Depends(operation_ledger)):
    check_permission(session, 'operator')
    require_csrf(request, session)
    report = ledger.retry(job_id, session['permission'], require_worker=True)
    return {'schema_version': 1, 'as_of': now_iso(), **report}


@app.get('/api/v1/jobs/{job_id}/logs', response_model=LogChunkDTO)
def job_logs(request: Request, job_id: str, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None, ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    report = ledger.read_logs(job_id, cursor, limit)
    return LogChunkDTO(**report)


@app.get('/api/v1/jobs/{job_id}/events', response_model=EventsDTO)
def job_events(request: Request, job_id: str, session=Depends(require_session), limit: int = 50, cursor: Optional[str] = None, ledger=Depends(operation_ledger)):
    check_permission(session, 'viewer')
    report = ledger.read_events(job_id, cursor, limit)
    return EventsDTO(**report)


@app.get('/api/v1/session', response_model=SessionDTO)
def read_session(request: Request, session=Depends(require_session)):
    return SessionDTO(**session)


@app.post('/api/v1/session', response_model=SessionDTO)
def issue_session(request: Request, body: SessionLoginDTO):
    if not origin_allowed(request):
        raise ApiError(403, 'origin_not_allowed', 'only localhost origin is allowed', retryable=False)
    ip = request.client.host if request.client else '127.0.0.1'
    session = session_store().login(body.password, ip=ip)
    response = JSONResponse(SessionDTO(**session).model_dump())
    response.set_cookie('management_session', session['session_id'], httponly=True,
                       samesite='strict', path='/api/v1', secure=request.url.scheme == 'https',
                       max_age=30 * 60)
    return response


@app.delete('/api/v1/session')
def clear_session(request: Request, session=Depends(require_session)):
    require_csrf(request, session)
    session_store().delete(session['session_id'])
    response = JSONResponse({'schema_version': 1, 'as_of': now_iso(), 'deleted': True})
    response.delete_cookie('management_session', path='/api/v1')
    return response

DASHBOARD_DIST = SOURCE_ROOT / 'dashboard/dist'
if DASHBOARD_DIST.is_dir():
    app.mount('/dashboard', StaticFiles(directory=DASHBOARD_DIST, html=True), name='dashboard')
