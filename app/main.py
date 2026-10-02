import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from shapely.geometry import LineString, shape
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import engine, get_db
from .engine import (
    EVENT_ENTER,
    EVENT_LEAVE,
    FenceConfig,
    GateLine,
    InspectionCheckpoint,
    ProjectedFence,
    TrackPoint,
    compute_clearance,
    compute_crossings,
    compute_events,
    compute_inspection_report,
    compute_occupancy_segments,
    compute_rendezvous_report,
    project_points,
)
from .models import (
    Base,
    CapacityRule,
    ClearanceTask,
    Crossing,
    DeviceState,
    Event,
    Fence,
    GateLineRow,
    InspectionCheckpointRow,
    InspectionRouteRow,
    RendezvousMemberRow,
    RendezvousTask,
    TrackPointRow,
)
from .schemas import (
    CapacityRuleCreate,
    CapacityRuleOut,
    ClearanceDeviceOut,
    ClearanceReportOut,
    ClearanceSummaryOut,
    ClearanceTaskCreate,
    ClearanceTaskOut,
    CrossingOut,
    DeviceStatusOut,
    EventOut,
    FenceConfigSchema,
    FenceCreate,
    FenceOut,
    FenceStateOut,
    GateLineCreate,
    GateLineOut,
    IngestResult,
    InspectionCheckpointOut,
    InspectionCheckpointResultOut,
    InspectionReportOut,
    InspectionRouteCreate,
    InspectionRouteOut,
    OccupancyOut,
    OccupancySegmentOut,
    PointBatch,
    RendezvousReportOut,
    RendezvousSegmentOut,
    RendezvousTaskCreate,
    RendezvousTaskOut,
)

# 摄入与重算在进程内串行化，保证同一设备的重算基于一致快照
INGEST_LOCK = threading.Lock()


def to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_iso(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(
    title="轨迹围栏事件 API",
    description="带孔多边形围栏 + 乱序轨迹重排 + 缓冲/停留去抖的 enter/leave/stay 事件服务，"
                "支持围栏容量核算、定向门线穿越核算、应急清场核验与协同到场核验",
    version="1.5.0",
    lifespan=lifespan,
)


# ---------- 内部逻辑 ----------

def recompute_device(db: Session, device_id: str) -> None:
    """按设备全量轨迹（按时间升序）重算该设备在所有围栏下的事件与当前状态。

    乱序点到达后重算可得到与按序到达一致的结果；重复提交因去重不产生新点，
    重算结果不变，因此不会重复产出事件。
    """
    rows = (
        db.query(TrackPointRow)
        .filter_by(device_id=device_id)
        .order_by(TrackPointRow.ts)
        .all()
    )
    points = [TrackPoint(ts=r.ts, lat=r.lat, lon=r.lon) for r in rows]
    fences = db.query(Fence).all()
    gates = db.query(GateLineRow).all()
    gates_by_fence: dict[int, list[GateLineRow]] = {}
    for g in gates:
        gates_by_fence.setdefault(g.fence_id, []).append(g)

    db.query(Event).filter_by(device_id=device_id).delete()
    db.query(DeviceState).filter_by(device_id=device_id).delete()
    db.query(Crossing).filter_by(device_id=device_id).delete()

    for f in fences:
        polygon = shape(json.loads(f.geometry))
        cfg = FenceConfig(
            enter_buffer_m=f.enter_buffer_m,
            exit_buffer_m=f.exit_buffer_m,
            enter_dwell_s=f.enter_dwell_s,
            exit_dwell_s=f.exit_dwell_s,
            stay_min_s=f.stay_min_s,
        )
        pf = ProjectedFence(f.id, polygon, cfg)
        events, inside, since = compute_events(pf, points)
        for e in events:
            db.add(Event(device_id=device_id, fence_id=e.fence_id, type=e.type, ts=e.ts))
        db.add(DeviceState(
            device_id=device_id,
            fence_id=f.id,
            state="inside" if inside else "outside",
            since=since,
        ))

        lat0, lon0 = pf.origin
        gate_lines = []
        for g in gates_by_fence.get(f.id, []):
            coords_ll = [tuple(c[:2]) for c in json.loads(g.geometry)["coordinates"]]
            gate_lines.append(GateLine(
                gate_id=g.id,
                coords=project_points(coords_ll, lat0, lon0),
                forward_name=g.forward_name,
                reverse_name=g.reverse_name,
                min_displacement_m=g.min_displacement_m,
            ))
        for c in compute_crossings(gate_lines, points, lat0, lon0):
            db.add(Crossing(
                device_id=device_id,
                fence_id=f.id,
                gate_id=c.gate_id,
                direction=c.direction,
                ts=c.ts,
                lat=c.lat,
                lon=c.lon,
            ))
    db.commit()


def fence_out(f: Fence) -> FenceOut:
    return FenceOut(
        id=f.id,
        name=f.name,
        geometry=json.loads(f.geometry),
        config=FenceConfigSchema(
            enter_buffer_m=f.enter_buffer_m,
            exit_buffer_m=f.exit_buffer_m,
            enter_dwell_s=f.enter_dwell_s,
            exit_dwell_s=f.exit_dwell_s,
            stay_min_s=f.stay_min_s,
        ),
        created_at=to_iso(f.created_at),
    )


# ---------- 围栏管理 ----------

@app.post("/fences", response_model=FenceOut, status_code=201)
def create_fence(body: FenceCreate, db: Session = Depends(get_db)):
    """创建带孔多边形围栏。geometry 为 GeoJSON Polygon，coordinates[0] 为外环，后续为孔洞。"""
    try:
        geom = shape(body.geometry)
    except Exception:
        raise HTTPException(status_code=422, detail="geometry 不是合法的 GeoJSON")
    if geom.geom_type != "Polygon" or geom.is_empty:
        raise HTTPException(status_code=422, detail="geometry 必须是 Polygon 类型")
    if not geom.is_valid:
        raise HTTPException(status_code=422, detail="多边形无效（自相交或孔洞越出外环）")

    fence = Fence(
        name=body.name,
        geometry=json.dumps(body.geometry),
        enter_buffer_m=body.config.enter_buffer_m,
        exit_buffer_m=body.config.exit_buffer_m,
        enter_dwell_s=body.config.enter_dwell_s,
        exit_dwell_s=body.config.exit_dwell_s,
        stay_min_s=body.config.stay_min_s,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(fence)
    db.commit()
    db.refresh(fence)

    # 新围栏对存量设备轨迹生效
    with INGEST_LOCK:
        device_ids = [r[0] for r in db.query(TrackPointRow.device_id).distinct()]
        for device_id in device_ids:
            recompute_device(db, device_id)

    return fence_out(fence)


@app.get("/fences", response_model=list[FenceOut])
def list_fences(db: Session = Depends(get_db)):
    return [fence_out(f) for f in db.query(Fence).order_by(Fence.id).all()]


@app.get("/fences/{fence_id}", response_model=FenceOut)
def get_fence(fence_id: int, db: Session = Depends(get_db)):
    fence = db.get(Fence, fence_id)
    if fence is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    return fence_out(fence)


@app.delete("/fences/{fence_id}", status_code=204)
def delete_fence(fence_id: int, db: Session = Depends(get_db)):
    fence = db.get(Fence, fence_id)
    if fence is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    db.query(Event).filter_by(fence_id=fence_id).delete()
    db.query(DeviceState).filter_by(fence_id=fence_id).delete()
    db.query(CapacityRule).filter_by(fence_id=fence_id).delete()
    db.query(Crossing).filter_by(fence_id=fence_id).delete()
    db.query(GateLineRow).filter_by(fence_id=fence_id).delete()
    db.query(ClearanceTask).filter_by(fence_id=fence_id).delete()
    task_ids = [
        t[0]
        for t in db.query(RendezvousTask.id).filter_by(fence_id=fence_id).all()
    ]
    if task_ids:
        db.query(RendezvousMemberRow).filter(
            RendezvousMemberRow.task_id.in_(task_ids)
        ).delete(synchronize_session=False)
    db.query(RendezvousTask).filter_by(fence_id=fence_id).delete()
    route_ids = [
        r[0]
        for r in db.query(InspectionRouteRow.id).filter_by(fence_id=fence_id).all()
    ]
    if route_ids:
        db.query(InspectionCheckpointRow).filter(
            InspectionCheckpointRow.route_id.in_(route_ids)
        ).delete(synchronize_session=False)
    db.query(InspectionRouteRow).filter_by(fence_id=fence_id).delete()
    db.delete(fence)
    db.commit()


# ---------- 轨迹点摄入 ----------

@app.post("/points", response_model=IngestResult)
def submit_points(batch: PointBatch, db: Session = Depends(get_db)):
    """批量提交轨迹点。乱序点按设备时间重排；(device_id, ts) 重复的点被幂等忽略。"""
    accepted = 0
    duplicates = 0
    devices: set[str] = set()
    seen: set[tuple[str, float]] = set()  # 本批次内已接收的 (device_id, ts)

    with INGEST_LOCK:
        for p in batch.points:
            ts = to_epoch(p.ts)
            key = (p.device_id, ts)
            if key in seen:
                duplicates += 1
                continue
            exists = (
                db.query(TrackPointRow.id)
                .filter_by(device_id=p.device_id, ts=ts)
                .first()
            )
            if exists:
                duplicates += 1
                continue
            seen.add(key)
            db.add(TrackPointRow(device_id=p.device_id, ts=ts, lat=p.lat, lon=p.lon))
            accepted += 1
            devices.add(p.device_id)
        db.commit()

        for device_id in devices:
            recompute_device(db, device_id)

    return IngestResult(accepted=accepted, duplicates=duplicates, devices_updated=len(devices))


# ---------- 事件与状态查询 ----------

@app.get("/events", response_model=list[EventOut])
def list_events(
    device_id: str | None = None,
    fence_id: int | None = None,
    type: Literal["enter", "leave", "stay"] | None = None,
    start: datetime | None = Query(None, description="起始时间（ISO 8601，含）"),
    end: datetime | None = Query(None, description="结束时间（ISO 8601，含）"),
    limit: int = Query(1000, le=10000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """按设备/围栏/事件类型/时间范围查询事件，按事件时间升序返回。"""
    q = db.query(Event)
    if device_id is not None:
        q = q.filter(Event.device_id == device_id)
    if fence_id is not None:
        q = q.filter(Event.fence_id == fence_id)
    if type is not None:
        q = q.filter(Event.type == type)
    if start is not None:
        q = q.filter(Event.ts >= to_epoch(start))
    if end is not None:
        q = q.filter(Event.ts <= to_epoch(end))
    rows = q.order_by(Event.ts, Event.id).limit(limit).offset(offset).all()
    return [
        EventOut(id=e.id, device_id=e.device_id, fence_id=e.fence_id, type=e.type, ts=to_iso(e.ts))
        for e in rows
    ]


@app.get("/devices/{device_id}/status", response_model=DeviceStatusOut)
def device_status(device_id: str, db: Session = Depends(get_db)):
    """查询设备当前相对各围栏的状态（inside/outside 及进入该状态的时间）。"""
    states = db.query(DeviceState).filter_by(device_id=device_id).all()
    fence_names = {f.id: f.name for f in db.query(Fence).all()}
    return DeviceStatusOut(
        device_id=device_id,
        fences=[
            FenceStateOut(
                fence_id=s.fence_id,
                fence_name=fence_names.get(s.fence_id, ""),
                state=s.state,
                since=to_iso(s.since),
            )
            for s in sorted(states, key=lambda s: s.fence_id)
        ],
    )


# ---------- 容量规则与占用核算 ----------

def capacity_rule_out(r: CapacityRule) -> CapacityRuleOut:
    return CapacityRuleOut(
        id=r.id,
        fence_id=r.fence_id,
        effective_from=to_iso(r.effective_from),
        max_occupancy=r.max_occupancy,
        created_at=to_iso(r.created_at),
    )


@app.post("/fences/{fence_id}/capacity-rules", response_model=CapacityRuleOut, status_code=201)
def create_capacity_rule(fence_id: int, body: CapacityRuleCreate, db: Session = Depends(get_db)):
    """新增容量规则：自 effective_from（含）起生效的正整数上限。

    同一围栏同一生效时刻只允许一条规则，重复提交返回 409。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    rule = CapacityRule(
        fence_id=fence_id,
        effective_from=to_epoch(body.effective_from),
        max_occupancy=body.max_occupancy,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="同一围栏同一生效时刻的容量规则已存在")
    db.refresh(rule)
    return capacity_rule_out(rule)


@app.get("/fences/{fence_id}/capacity-rules", response_model=list[CapacityRuleOut])
def list_capacity_rules(fence_id: int, db: Session = Depends(get_db)):
    """按生效时刻升序列出围栏的容量规则。"""
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    rules = (
        db.query(CapacityRule)
        .filter_by(fence_id=fence_id)
        .order_by(CapacityRule.effective_from)
        .all()
    )
    return [capacity_rule_out(r) for r in rules]


@app.delete("/fences/{fence_id}/capacity-rules/{rule_id}", status_code=204)
def delete_capacity_rule(fence_id: int, rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(CapacityRule, rule_id)
    if rule is None or rule.fence_id != fence_id:
        raise HTTPException(status_code=404, detail="容量规则不存在")
    db.delete(rule)
    db.commit()


@app.get("/fences/{fence_id}/occupancy", response_model=OccupancyOut)
def fence_occupancy(
    fence_id: int,
    start: datetime = Query(description="起始时间（ISO 8601，含）"),
    end: datetime = Query(description="结束时间（ISO 8601，不含）"),
    db: Session = Depends(get_db),
):
    """查询 [start, end) 内的占用核算：状态恒定的分段序列 + 超限明细。

    由 enter/leave 事件还原各设备的半开在场区间（越界部分截断到查询窗口）；
    同一时刻的设备进出与容量规则切换按一个原子边界处理；超限分段给出上限、
    实际人数、超额数与按 ID 排序的在场设备。占用在查询时由事件现算，
    补录乱序轨迹点触发事件重算后，再次查询即得到最新结果。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    start_ts, end_ts = to_epoch(start), to_epoch(end)
    if not start_ts < end_ts:
        raise HTTPException(status_code=422, detail="start 必须早于 end")

    events = (
        db.query(Event)
        .filter(Event.fence_id == fence_id, Event.type.in_([EVENT_ENTER, EVENT_LEAVE]))
        .all()
    )
    rules = db.query(CapacityRule).filter_by(fence_id=fence_id).all()
    segments = compute_occupancy_segments(
        [(e.device_id, e.type, e.ts) for e in events],
        [(r.effective_from, r.max_occupancy) for r in rules],
        start_ts,
        end_ts,
    )
    return OccupancyOut(
        fence_id=fence_id,
        start=to_iso(start_ts),
        end=to_iso(end_ts),
        segments=[
            OccupancySegmentOut(
                start=to_iso(s.start),
                end=to_iso(s.end),
                count=s.count,
                limit=s.limit,
                exceeded=s.exceeded,
                excess=s.excess,
                devices=list(s.devices) if s.exceeded else None,
            )
            for s in segments
        ],
    )


# ---------- 门线与穿越核算 ----------


def gate_out(g: GateLineRow) -> GateLineOut:
    return GateLineOut(
        id=g.id,
        fence_id=g.fence_id,
        geometry=json.loads(g.geometry),
        forward_name=g.forward_name,
        reverse_name=g.reverse_name,
        min_displacement_m=g.min_displacement_m,
        created_at=to_iso(g.created_at),
    )


def _gate_line_within_polygon(geometry: dict, polygon) -> LineString:
    """校验门线 GeoJSON：简单 LineString，且整条线位于围栏有效区域（外环内、孔洞外）。

    返回解析后的 shapely LineString；任何不满足条件的情况抛 ValueError。
    """
    try:
        line = shape(geometry)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("geometry 不是合法的 GeoJSON") from exc
    if line.geom_type != "LineString" or line.is_empty:
        raise ValueError("geometry 必须是非空 LineString 类型")
    if len(line.coords) < 2:
        raise ValueError("LineString 至少需要两个坐标点")
    if len(set(line.coords)) != len(line.coords):
        raise ValueError("LineString 不得包含重复坐标点")
    if not line.is_simple or line.length <= 0:
        raise ValueError("LineString 必须是不自交的简单折线")
    # covers：整条线（含落在围栏外边界上的部分）都在多边形有效区域内；
    # 孔洞内部不属于多边形区域，穿入孔洞即被拒绝。
    if not polygon.covers(line):
        raise ValueError("门线整条必须位于围栏有效区域内，不得越出围栏或穿过孔洞")
    # 注意：孔洞内环也是多边形 boundary 的一部分，covers 会把贴在孔洞边界上的
    # 线判为在内，因此额外要求门线不得与任何孔洞边界相交（接触也不允许）。
    for ring in polygon.interiors:
        if line.intersects(ring):
            raise ValueError("门线整条必须位于围栏有效区域内，不得越出围栏或穿过孔洞")
    return line


@app.post("/fences/{fence_id}/gates", response_model=GateLineOut, status_code=201)
def create_gate(fence_id: int, body: GateLineCreate, db: Session = Depends(get_db)):
    """在指定围栏内创建定向门线。

    geometry 为简单 GeoJSON LineString，整条线必须位于围栏有效区域内
    （不得越出外环或穿过孔洞）。方向按坐标顺序判定：沿坐标顺序看，
    右->左为正向（forward_name），左->右为反向（reverse_name）。
    创建后对所有已有设备轨迹重算穿越，因此先有点后建门线也能补出结果。
    """
    fence = db.get(Fence, fence_id)
    if fence is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    polygon = shape(json.loads(fence.geometry))
    try:
        _gate_line_within_polygon(body.geometry, polygon)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    gate = GateLineRow(
        fence_id=fence_id,
        geometry=json.dumps(body.geometry),
        forward_name=body.forward_name,
        reverse_name=body.reverse_name,
        min_displacement_m=body.min_displacement_m,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(gate)
    db.commit()
    db.refresh(gate)

    # 新门线对存量设备轨迹生效
    with INGEST_LOCK:
        device_ids = [r[0] for r in db.query(TrackPointRow.device_id).distinct()]
        for device_id in device_ids:
            recompute_device(db, device_id)

    return gate_out(gate)


@app.get("/fences/{fence_id}/gates", response_model=list[GateLineOut])
def list_gates(fence_id: int, db: Session = Depends(get_db)):
    """列出门线（按 id 升序）。"""
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    rows = db.query(GateLineRow).filter_by(fence_id=fence_id).order_by(GateLineRow.id).all()
    return [gate_out(g) for g in rows]


@app.delete("/fences/{fence_id}/gates/{gate_id}", status_code=204)
def delete_gate(fence_id: int, gate_id: int, db: Session = Depends(get_db)):
    gate = db.get(GateLineRow, gate_id)
    if gate is None or gate.fence_id != fence_id:
        raise HTTPException(status_code=404, detail="门线不存在")
    referenced = db.query(InspectionCheckpointRow.id).filter_by(gate_id=gate_id).first()
    if referenced is not None:
        raise HTTPException(status_code=409, detail="门线被巡检路线引用，不能删除")
    db.query(Crossing).filter_by(gate_id=gate_id).delete()
    db.delete(gate)
    db.commit()


@app.get("/crossings", response_model=list[CrossingOut])
def list_crossings(
    device_id: str | None = None,
    gate_id: int | None = None,
    direction: Literal["forward", "reverse"] | None = None,
    start: datetime | None = Query(None, description="起始时间（ISO 8601，含）"),
    end: datetime | None = Query(None, description="结束时间（ISO 8601，不含）"),
    fence_id: int | None = None,
    limit: int = Query(1000, le=10000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查询门线穿越记录，按 (穿越时刻, 门线 ID) 升序返回。

    时间窗口为半开区间 [start, end)。穿越记录在每次轨迹摄入后按设备全量
    重算并整体替换，补录乱序点后结果随之更新且不会产生重复记录。
    """
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="start 与 end 必须同时提供")
    if start is not None and not to_epoch(start) < to_epoch(end):
        raise HTTPException(status_code=422, detail="start 必须早于 end")
    if gate_id is not None:
        gate = db.get(GateLineRow, gate_id)
        if gate is None:
            raise HTTPException(status_code=404, detail="门线不存在")
        if fence_id is not None and fence_id != gate.fence_id:
            raise HTTPException(status_code=404, detail="门线不属于该围栏")

    q = db.query(Crossing)
    if device_id is not None:
        q = q.filter(Crossing.device_id == device_id)
    if gate_id is not None:
        q = q.filter(Crossing.gate_id == gate_id)
    if fence_id is not None:
        q = q.filter(Crossing.fence_id == fence_id)
    if direction is not None:
        q = q.filter(Crossing.direction == direction)
    if start is not None:
        q = q.filter(Crossing.ts >= to_epoch(start))
    if end is not None:
        q = q.filter(Crossing.ts < to_epoch(end))
    rows = q.order_by(Crossing.ts, Crossing.gate_id, Crossing.id).limit(limit).offset(offset).all()

    gate_rows = {g.id: g for g in db.query(GateLineRow).all()}
    result = []
    for c in rows:
        g = gate_rows.get(c.gate_id)
        if g is None:  # 门线被删除时其穿越应已级联清除，防御处理
            continue
        direction_name = g.forward_name if c.direction == "forward" else g.reverse_name
        result.append(CrossingOut(
            id=c.id,
            device_id=c.device_id,
            fence_id=c.fence_id,
            gate_id=c.gate_id,
            direction=c.direction,
            direction_name=direction_name,
            ts=to_iso(c.ts),
            lat=c.lat,
            lon=c.lon,
        ))
    return result


# ---------- 应急清场核验 ----------


def clearance_task_out(task: ClearanceTask) -> ClearanceTaskOut:
    return ClearanceTaskOut(
        id=task.id,
        fence_id=task.fence_id,
        name=task.name,
        start_at=to_iso(task.start_ts),
        deadline=to_iso(task.deadline_ts),
        created_at=to_iso(task.created_at),
    )


@app.post("/fences/{fence_id}/clearance-tasks", response_model=ClearanceTaskOut, status_code=201)
def create_clearance_task(fence_id: int, body: ClearanceTaskCreate, db: Session = Depends(get_db)):
    """创建应急清场任务：含名称、开始时刻与截止时刻，开始必须严格早于截止。

    核验报告不固化，查询时由当前 enter/leave 事件实时重建，因此补录乱序
    轨迹点后再次查询即得到同步更新的名单与结果。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    start_ts, deadline_ts = to_epoch(body.start_at), to_epoch(body.deadline)
    if not start_ts < deadline_ts:
        raise HTTPException(status_code=422, detail="开始时刻必须早于截止时刻")

    task = ClearanceTask(
        fence_id=fence_id,
        name=body.name,
        start_ts=start_ts,
        deadline_ts=deadline_ts,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return clearance_task_out(task)


@app.get("/fences/{fence_id}/clearance-tasks", response_model=list[ClearanceTaskOut])
def list_clearance_tasks(fence_id: int, db: Session = Depends(get_db)):
    """列出围栏的清场任务（按创建先后，即 id 升序）。"""
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    rows = (
        db.query(ClearanceTask)
        .filter_by(fence_id=fence_id)
        .order_by(ClearanceTask.id)
        .all()
    )
    return [clearance_task_out(t) for t in rows]


def _get_clearance_task(db: Session, fence_id: int, task_id: int) -> ClearanceTask:
    task = db.get(ClearanceTask, task_id)
    if task is None or task.fence_id != fence_id:
        raise HTTPException(status_code=404, detail="清场任务不存在")
    return task


@app.delete("/fences/{fence_id}/clearance-tasks/{task_id}", status_code=204)
def delete_clearance_task(fence_id: int, task_id: int, db: Session = Depends(get_db)):
    task = _get_clearance_task(db, fence_id, task_id)
    db.delete(task)
    db.commit()


@app.get(
    "/fences/{fence_id}/clearance-tasks/{task_id}/report",
    response_model=ClearanceReportOut,
)
def clearance_report(fence_id: int, task_id: int, db: Session = Depends(get_db)):
    """查询清场核验报告：每次由当前 enter/leave 事件实时重建，不保留旧快照。

    在场区间一律按半开区间 [enter, leave) 处理：起始时刻恰在 enter 的设备
    计入起始名单，恰在 leave 的排除；截止时刻沿用同一套半开边界。只核对
    起始名单：窗口内离开后又进入的设备必须再次离开并持续在外至截止，
    才算按时清场。返回每台设备的 cleared/remaining 状态与最后有效离开
    时刻，并汇总起始、按时、未清场人数；设备按 ID 排序。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    task = _get_clearance_task(db, fence_id, task_id)

    events = (
        db.query(Event)
        .filter(Event.fence_id == fence_id, Event.type.in_([EVENT_ENTER, EVENT_LEAVE]))
        .all()
    )
    report = compute_clearance(
        [(e.device_id, e.type, e.ts) for e in events],
        task.start_ts,
        task.deadline_ts,
    )
    return ClearanceReportOut(
        **clearance_task_out(task).model_dump(),
        summary=ClearanceSummaryOut(
            start_count=report.start_count,
            cleared_count=report.cleared_count,
            remaining_count=report.remaining_count,
        ),
        devices=[
            ClearanceDeviceOut(
                device_id=r.device_id,
                status=r.status,
                last_leave_at=to_iso(r.last_leave_ts),
            )
            for r in report.results
        ],
    )


# ---------- 有序门线巡检路线 ----------


def _get_route(db: Session, fence_id: int, route_id: int) -> InspectionRouteRow:
    route = db.get(InspectionRouteRow, route_id)
    if route is None or route.fence_id != fence_id:
        raise HTTPException(status_code=404, detail="巡检路线不存在")
    return route


def inspection_route_out(
    db: Session,
    route: InspectionRouteRow,
    checkpoints: list[InspectionCheckpointRow] | None = None,
    gates_by_id: dict[int, GateLineRow] | None = None,
) -> InspectionRouteOut:
    if checkpoints is None:
        checkpoints = (
            db.query(InspectionCheckpointRow)
            .filter_by(route_id=route.id)
            .order_by(InspectionCheckpointRow.position)
            .all()
        )
    if gates_by_id is None:
        gate_ids = {cp.gate_id for cp in checkpoints}
        gates_by_id = {
            g.id: g
            for g in db.query(GateLineRow).filter(GateLineRow.id.in_(gate_ids)).all()
        } if gate_ids else {}

    cps_out = []
    for cp in checkpoints:
        g = gates_by_id.get(cp.gate_id)
        direction_name = ""
        if g is not None:
            direction_name = g.forward_name if cp.direction == "forward" else g.reverse_name
        cps_out.append(InspectionCheckpointOut(
            position=cp.position,
            gate_id=cp.gate_id,
            direction=cp.direction,
            direction_name=direction_name,
            max_interval_s=cp.max_interval_s,
        ))
    return InspectionRouteOut(
        id=route.id,
        fence_id=route.fence_id,
        name=route.name,
        checkpoints=cps_out,
        created_at=to_iso(route.created_at),
    )


@app.post("/fences/{fence_id}/inspection-routes", response_model=InspectionRouteOut, status_code=201)
def create_inspection_route(
    fence_id: int, body: InspectionRouteCreate, db: Session = Depends(get_db)
):
    """创建有序门线巡检路线：2～8 个按顺序排列的门线检查点。

    每个检查点指定已有门线、要求的穿越方向与（除首点外）与上一点的最长
    间隔。门线必须全部存在且属于该围栏，路线内不得重复；首点不设间隔。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")

    gate_ids = {cp.gate_id for cp in body.checkpoints}
    gates = db.query(GateLineRow).filter(GateLineRow.id.in_(gate_ids)).all()
    gates_by_id = {g.id: g for g in gates}
    missing = sorted(gate_ids - gates_by_id.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"门线不存在: {missing}")
    foreign = sorted(gid for gid, g in gates_by_id.items() if g.fence_id != fence_id)
    if foreign:
        raise HTTPException(status_code=422, detail=f"门线不属于该围栏: {foreign}")

    route = InspectionRouteRow(
        fence_id=fence_id,
        name=body.name,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(route)
    db.flush()  # 取得 route.id
    for position, cp in enumerate(body.checkpoints):
        db.add(InspectionCheckpointRow(
            route_id=route.id,
            position=position,
            gate_id=cp.gate_id,
            direction=cp.direction,
            max_interval_s=cp.max_interval_s,
        ))
    db.commit()
    db.refresh(route)
    checkpoints = (
        db.query(InspectionCheckpointRow)
        .filter_by(route_id=route.id)
        .order_by(InspectionCheckpointRow.position)
        .all()
    )
    return inspection_route_out(db, route, checkpoints, gates_by_id)


@app.get("/fences/{fence_id}/inspection-routes", response_model=list[InspectionRouteOut])
def list_inspection_routes(fence_id: int, db: Session = Depends(get_db)):
    """列出围栏的巡检路线（按 id 升序），每条含按顺序排列的检查点。"""
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    routes = (
        db.query(InspectionRouteRow)
        .filter_by(fence_id=fence_id)
        .order_by(InspectionRouteRow.id)
        .all()
    )
    all_gates = {g.id: g for g in db.query(GateLineRow).all()}
    cps = (
        db.query(InspectionCheckpointRow)
        .filter(InspectionCheckpointRow.route_id.in_([r.id for r in routes]))
        .order_by(InspectionCheckpointRow.position)
        .all()
        if routes else []
    )
    cps_by_route: dict[int, list[InspectionCheckpointRow]] = {}
    for cp in cps:
        cps_by_route.setdefault(cp.route_id, []).append(cp)
    return [
        inspection_route_out(db, r, cps_by_route.get(r.id, []), all_gates)
        for r in routes
    ]


def _delete_inspection_route(db: Session, route: InspectionRouteRow) -> None:
    db.query(InspectionCheckpointRow).filter_by(route_id=route.id).delete()
    db.delete(route)
    db.commit()


@app.delete("/fences/{fence_id}/inspection-routes/{route_id}", status_code=204)
def delete_inspection_route(fence_id: int, route_id: int, db: Session = Depends(get_db)):
    route = _get_route(db, fence_id, route_id)
    _delete_inspection_route(db, route)


@app.get(
    "/fences/{fence_id}/inspection-routes/{route_id}/report",
    response_model=InspectionReportOut,
)
def inspection_route_report(
    fence_id: int,
    route_id: int,
    device_id: str = Query(description="待核验设备 ID"),
    start: datetime = Query(description="核验窗口起始时间（ISO 8601，含）"),
    end: datetime = Query(description="核验窗口结束时间（ISO 8601，不含）"),
    db: Session = Depends(get_db),
):
    """核验设备在 [start, end) 内是否按路线顺序通过各检查点。

    报告每次由当前穿越记录重放实时计算，不保留旧快照：补录乱序轨迹点触发
    穿越重算后，再次查询即按最新穿越结果给出通过时刻或失败原因
    （超时 / 顺序违规 / 未到达）及首个违规时刻。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    route = _get_route(db, fence_id, route_id)
    start_ts, end_ts = to_epoch(start), to_epoch(end)
    if not start_ts < end_ts:
        raise HTTPException(status_code=422, detail="start 必须早于 end")

    checkpoints = (
        db.query(InspectionCheckpointRow)
        .filter_by(route_id=route.id)
        .order_by(InspectionCheckpointRow.position)
        .all()
    )
    gate_ids = [cp.gate_id for cp in checkpoints]
    gates = db.query(GateLineRow).filter(GateLineRow.id.in_(gate_ids)).all()
    gates_by_id = {g.id: g for g in gates}

    crossing_rows = (
        db.query(Crossing)
        .filter(
            Crossing.device_id == device_id,
            Crossing.fence_id == fence_id,
            Crossing.gate_id.in_(gate_ids),
            Crossing.ts >= start_ts,
            Crossing.ts < end_ts,
        )
        .order_by(Crossing.ts, Crossing.gate_id, Crossing.id)
        .all()
    )
    report = compute_inspection_report(
        route_id=route.id,
        device_id=device_id,
        checkpoints=[
            InspectionCheckpoint(
                position=cp.position,
                gate_id=cp.gate_id,
                direction=cp.direction,
                max_interval_s=cp.max_interval_s,
            )
            for cp in checkpoints
        ],
        crossings=[(c.gate_id, c.direction, c.ts) for c in crossing_rows],
        start=start_ts,
        end=end_ts,
    )

    base = inspection_route_out(db, route, checkpoints, gates_by_id).model_dump()
    return InspectionReportOut(
        **base,
        device_id=device_id,
        start=to_iso(start_ts),
        end=to_iso(end_ts),
        passed=report.passed,
        first_violation_at=to_iso(report.first_violation_ts),
        results=[
            InspectionCheckpointResultOut(
                position=r.position,
                gate_id=r.gate_id,
                direction=r.direction,
                direction_name=(
                    gates_by_id[r.gate_id].forward_name
                    if r.direction == "forward"
                    else gates_by_id[r.gate_id].reverse_name
                ),
                status=r.status,
                passed_at=to_iso(r.passed_ts),
                failed_at=to_iso(r.failed_ts),
            )
            for r in report.results
        ],
    )


# ---------- 协同到场核验 ----------


def _rendezvous_members(db: Session, task_id: int) -> list[str]:
    rows = (
        db.query(RendezvousMemberRow)
        .filter_by(task_id=task_id)
        .order_by(RendezvousMemberRow.position)
        .all()
    )
    return [r.device_id for r in rows]


def rendezvous_task_out(db: Session, task: RendezvousTask) -> RendezvousTaskOut:
    return RendezvousTaskOut(
        id=task.id,
        fence_id=task.fence_id,
        name=task.name,
        members=_rendezvous_members(db, task.id),
        start_at=to_iso(task.start_ts),
        end_at=to_iso(task.end_ts),
        min_co_presence_s=task.min_co_presence_s,
        created_at=to_iso(task.created_at),
    )


def _get_rendezvous_task(db: Session, fence_id: int, task_id: int) -> RendezvousTask:
    task = db.get(RendezvousTask, task_id)
    if task is None or task.fence_id != fence_id:
        raise HTTPException(status_code=404, detail="协同任务不存在")
    return task


@app.post(
    "/fences/{fence_id}/rendezvous-tasks",
    response_model=RendezvousTaskOut,
    status_code=201,
)
def create_rendezvous_task(
    fence_id: int, body: RendezvousTaskCreate, db: Session = Depends(get_db)
):
    """创建协同到场任务：2～8 台不重复设备 + 有效窗口 + 最短共同在场时长。

    成员数量为 2～8、设备 ID 不得重复、开始时刻必须严格早于结束时刻，
    最短共同在场时长为正数，否则返回 422；围栏不存在返回 404。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    start_ts, end_ts = to_epoch(body.start_at), to_epoch(body.end_at)
    if not start_ts < end_ts:
        raise HTTPException(status_code=422, detail="开始时刻必须早于结束时刻")

    task = RendezvousTask(
        fence_id=fence_id,
        name=body.name,
        start_ts=start_ts,
        end_ts=end_ts,
        min_co_presence_s=body.min_co_presence_s,
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    db.add(task)
    db.flush()  # 取得 task.id
    for position, device_id in enumerate(body.members):
        db.add(RendezvousMemberRow(
            task_id=task.id, position=position, device_id=device_id
        ))
    db.commit()
    db.refresh(task)
    return rendezvous_task_out(db, task)


@app.get(
    "/fences/{fence_id}/rendezvous-tasks",
    response_model=list[RendezvousTaskOut],
)
def list_rendezvous_tasks(fence_id: int, db: Session = Depends(get_db)):
    """列出围栏的协同到场任务（按 id 升序），成员按提交顺序。"""
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    tasks = (
        db.query(RendezvousTask)
        .filter_by(fence_id=fence_id)
        .order_by(RendezvousTask.id)
        .all()
    )
    return [rendezvous_task_out(db, t) for t in tasks]


@app.delete(
    "/fences/{fence_id}/rendezvous-tasks/{task_id}",
    status_code=204,
)
def delete_rendezvous_task(fence_id: int, task_id: int, db: Session = Depends(get_db)):
    task = _get_rendezvous_task(db, fence_id, task_id)
    db.query(RendezvousMemberRow).filter_by(task_id=task.id).delete()
    db.delete(task)
    db.commit()


@app.get(
    "/fences/{fence_id}/rendezvous-tasks/{task_id}/report",
    response_model=RendezvousReportOut,
)
def rendezvous_report(fence_id: int, task_id: int, db: Session = Depends(get_db)):
    """查询协同到场核验报告：每次由当前 enter/leave 事件实时重建，不保留旧结果。

    从事件还原各成员的半开在场区间 [enter, leave) 并截断到有效窗口，
    按时间扫描全体成员同时在场的连续片段：同刻 enter/leave 按半开边界
    处理，成员反复进出形成多个候选；只有窗口内持续达到（含）最短共同
    在场时长的片段才算通过，返回片段起止、持续秒数与成员列表。
    无达标片段时 passed=false、segments 为空。
    """
    if db.get(Fence, fence_id) is None:
        raise HTTPException(status_code=404, detail="围栏不存在")
    task = _get_rendezvous_task(db, fence_id, task_id)
    members = _rendezvous_members(db, task.id)

    events = (
        db.query(Event)
        .filter(
            Event.fence_id == fence_id,
            Event.type.in_([EVENT_ENTER, EVENT_LEAVE]),
            Event.device_id.in_(members),
        )
        .all()
    )
    report = compute_rendezvous_report(
        [(e.device_id, e.type, e.ts) for e in events],
        members,
        task.start_ts,
        task.end_ts,
        task.min_co_presence_s,
    )
    base = rendezvous_task_out(db, task).model_dump()
    return RendezvousReportOut(
        **base,
        passed=report.passed,
        segments=[
            RendezvousSegmentOut(
                start=to_iso(s.start),
                end=to_iso(s.end),
                duration_s=s.duration_s,
                members=list(s.members),
            )
            for s in report.segments
        ],
    )


@app.get("/health")
def health():
    return {"status": "ok"}