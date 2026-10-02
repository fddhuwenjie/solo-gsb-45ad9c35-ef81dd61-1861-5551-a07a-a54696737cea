from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 10, 2, 9, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

INSIDE = (0.005, 0.005)
OUTSIDE = (0.02, 0.02)

START_SEC = 100
END_SEC = 400
MIN_DUR = 60.0


def pt(device, sec, inside=True):
    lat, lon = INSIDE if inside else OUTSIDE
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def dts(sec):
    return BASE + timedelta(seconds=sec)


def create_fence(client):
    r = client.post("/fences", json={"name": "协同测试围栏", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def create_task(
    client,
    fence_id,
    devices=("dev-a", "dev-b"),
    name="双人到场",
    start=START_SEC,
    end=END_SEC,
    min_duration=MIN_DUR,
):
    r = client.post(
        f"/fences/{fence_id}/coattendance-tasks",
        json={
            "name": name,
            "device_ids": list(devices),
            "start_at": iso(start),
            "end_at": iso(end),
            "min_duration_s": min_duration,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def report(client, fence_id, task_id):
    r = client.get(f"/fences/{fence_id}/coattendance-tasks/{task_id}/report")
    assert r.status_code == 200, r.text
    return r.json()


def test_create_task_validation_and_crud(client):
    fence_id = create_fence(client)

    # 围栏不存在 -> 404
    r = client.post(
        "/fences/999/coattendance-tasks",
        json={
            "name": "x",
            "device_ids": ["a", "b"],
            "start_at": iso(0),
            "end_at": iso(10),
            "min_duration_s": 5,
        },
    )
    assert r.status_code == 404

    # 成员数量：1 个、9 个均 422（要求 2～8）
    r = client.post(
        f"/fences/{fence_id}/coattendance-tasks",
        json={
            "name": "too-few",
            "device_ids": ["only-one"],
            "start_at": iso(0),
            "end_at": iso(10),
            "min_duration_s": 5,
        },
    )
    assert r.status_code == 422
    r = client.post(
        f"/fences/{fence_id}/coattendance-tasks",
        json={
            "name": "too-many",
            "device_ids": [f"d{i}" for i in range(9)],
            "start_at": iso(0),
            "end_at": iso(10),
            "min_duration_s": 5,
        },
    )
    assert r.status_code == 422

    # 设备 ID 重复 -> 422
    r = client.post(
        f"/fences/{fence_id}/coattendance-tasks",
        json={
            "name": "dup",
            "device_ids": ["a", "b", "a"],
            "start_at": iso(0),
            "end_at": iso(10),
            "min_duration_s": 5,
        },
    )
    assert r.status_code == 422

    # 空设备 ID / 纯空白 -> 422
    r = client.post(
        f"/fences/{fence_id}/coattendance-tasks",
        json={
            "name": "empty",
            "device_ids": ["a", "   "],
            "start_at": iso(0),
            "end_at": iso(10),
            "min_duration_s": 5,
        },
    )
    assert r.status_code == 422

    # 窗口无效：起止相等、倒置 -> 422
    for s, e in ((100, 100), (200, 100)):
        r = client.post(
            f"/fences/{fence_id}/coattendance-tasks",
            json={
                "name": "bad-window",
                "device_ids": ["a", "b"],
                "start_at": iso(s),
                "end_at": iso(e),
                "min_duration_s": 5,
            },
        )
        assert r.status_code == 422

    # min_duration_s 非正 -> 422
    for dur in (0, -1):
        r = client.post(
            f"/fences/{fence_id}/coattendance-tasks",
            json={
                "name": "bad-dur",
                "device_ids": ["a", "b"],
                "start_at": iso(0),
                "end_at": iso(10),
                "min_duration_s": dur,
            },
        )
        assert r.status_code == 422

    # 合法创建：成员按提交顺序返回
    task = create_task(client, fence_id, devices=("dev-b", "dev-a"), name="协同A")
    assert task["fence_id"] == fence_id
    assert task["device_ids"] == ["dev-b", "dev-a"]
    assert datetime.fromisoformat(task["start_at"]) == dts(START_SEC)
    assert datetime.fromisoformat(task["end_at"]) == dts(END_SEC)
    assert task["min_duration_s"] == MIN_DUR

    # 列表按 id 升序
    create_task(client, fence_id, devices=("c", "d"), name="协同B")
    tasks = client.get(f"/fences/{fence_id}/coattendance-tasks").json()
    assert [t["name"] for t in tasks] == ["协同A", "协同B"]

    # 删除；重复删除 / 不属于该围栏 / 查报告 -> 404
    tid = tasks[0]["id"]
    assert client.delete(f"/fences/{fence_id}/coattendance-tasks/{tid}").status_code == 204
    assert client.delete(f"/fences/{fence_id}/coattendance-tasks/{tid}").status_code == 404
    assert client.get(
        f"/fences/{fence_id}/coattendance-tasks/{tid}/report"
    ).status_code == 404
    other = create_fence(client)
    assert client.delete(
        f"/fences/{other}/coattendance-tasks/{tasks[1]['id']}"
    ).status_code == 404
    assert client.get(
        f"/fences/{other}/coattendance-tasks/{tasks[1]['id']}/report"
    ).status_code == 404


def test_basic_passed_segment(client):
    fence_id = create_fence(client)
    # a: 0 进入，300 离开；b: 120 进入，240 离开 -> 共同 [120,240) 持续 120s
    submit(client, [
        pt("dev-a", 0), pt("dev-a", 300, inside=False),
        pt("dev-b", 120), pt("dev-b", 240, inside=False),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(120)
    assert datetime.fromisoformat(seg["end"]) == dts(240)
    assert seg["duration_s"] == 120.0
    assert seg["members"] == ["dev-a", "dev-b"]


def test_duration_below_threshold_fails(client):
    fence_id = create_fence(client)
    # 共同在场仅 50s，不足 60s 阈值
    submit(client, [
        pt("dev-a", 0), pt("dev-a", 300, inside=False),
        pt("dev-b", 120), pt("dev-b", 170, inside=False),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # 恰好等于阈值（60s）算通过
    submit(client, [
        pt("dev-b", 200), pt("dev-b", 260, inside=False),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    assert rep["segments"][0]["duration_s"] == 60.0
    assert datetime.fromisoformat(rep["segments"][0]["start"]) == dts(200)
    assert datetime.fromisoformat(rep["segments"][0]["end"]) == dts(260)


def test_window_clipping_and_member_never_enters(client):
    fence_id = create_fence(client)
    # a 从窗口前一直在场到窗口后；b 在窗口内 150~350 在场。
    # 共同片段被截断为 [150, 350)；c 从未在场。
    submit(client, [
        pt("dev-a", 0),
        pt("dev-b", 150), pt("dev-b", 350, inside=False),
    ])
    task = create_task(client, fence_id, devices=("dev-a", "dev-b", "dev-c"))
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # c 补录后在窗口内全程在场：共同片段 [150,350) 持续 200s
    submit(client, [pt("dev-c", 50)])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(150)
    assert datetime.fromisoformat(seg["end"]) == dts(350)
    assert seg["duration_s"] == 200.0
    assert seg["members"] == ["dev-a", "dev-b", "dev-c"]

    # 起点恰在窗口开始的 enter 计入；共同片段从窗口边界开始
    submit(client, [
        pt("dev-b", 360),  # b 再次进入并持续到窗口结束后
    ])
    rep = report(client, fence_id, task["id"])
    # b 的区间变为 [150,350) 与 [360,+inf)，共同片段仍是 [150,350)
    assert len(rep["segments"]) == 1


def test_half_open_boundaries(client):
    fence_id = create_fence(client)
    # a: [0,200) 离开；b: [0,350) 全程在场；c: 200 同刻进入（之前在外）。
    # 200 处 a 的区间止、c 的区间起 —— 半开边界下同刻换人。
    submit(client, [
        pt("dev-a", 0), pt("dev-a", 200, inside=False),
        pt("dev-b", 0), pt("dev-b", 350, inside=False),
        pt("dev-c", 50, inside=False), pt("dev-c", 200), pt("dev-c", 350, inside=False),
    ])

    # a 与 c 在 200 同刻换人，窗口 [200,300) 内从无共同在场
    task = create_task(client, fence_id, devices=("dev-a", "dev-c"),
                       name="换人-失败", start=200, end=300, min_duration=1)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # 三个成员：200 前 {a,b}、200 后 {b,c}，全员集合从未集齐
    task = create_task(client, fence_id, devices=("dev-a", "dev-b", "dev-c"),
                       name="锚点换人", start=199, end=300, min_duration=1)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # a、b 的共同片段在 200 处被窗口/leave 截断：[199,200) 恰好 1s 达标，
    # 不延伸到 200 之后（b 虽继续在场，a 的区间止端不含 200）
    task = create_task(client, fence_id, devices=("dev-a", "dev-b"),
                       name="止端截断", start=199, end=300, min_duration=1)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(199)
    assert datetime.fromisoformat(seg["end"]) == dts(200)
    assert seg["duration_s"] == 1.0

    # c 恰在窗口起始 200 的 enter 计入半开始端：b、c 共同 [200,300)
    task = create_task(client, fence_id, devices=("dev-b", "dev-c"),
                       name="起端计入", start=200, end=300, min_duration=1)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(200)
    assert datetime.fromisoformat(seg["end"]) == dts(300)
    assert seg["duration_s"] == 100.0


def test_repeated_in_out_produces_multiple_candidates(client):
    fence_id = create_fence(client)
    # a 全程在场；b 反复进出：[120,180)(60s)、[240,290)(50s)、[300,380)(80s)
    submit(client, [
        pt("dev-a", 0),
        pt("dev-b", 120), pt("dev-b", 180, inside=False),
        pt("dev-b", 240), pt("dev-b", 290, inside=False),
        pt("dev-b", 300), pt("dev-b", 380, inside=False),
    ])
    task = create_task(client, fence_id, min_duration=60)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    # 只有 60s 与 80s 两个片段达标，50s 的被排除；按起始时刻升序
    assert len(rep["segments"]) == 2
    s0, s1 = rep["segments"]
    assert datetime.fromisoformat(s0["start"]) == dts(120)
    assert datetime.fromisoformat(s0["end"]) == dts(180)
    assert s0["duration_s"] == 60.0
    assert datetime.fromisoformat(s1["start"]) == dts(300)
    assert datetime.fromisoformat(s1["end"]) == dts(380)
    assert s1["duration_s"] == 80.0
    for s in rep["segments"]:
        assert s["members"] == ["dev-a", "dev-b"]


def test_report_rebuilt_after_out_of_order_backfill(client):
    fence_id = create_fence(client)
    task = create_task(client, fence_id)

    # 初次：a、b 共同在场仅 30s -> 失败
    submit(client, [
        pt("dev-a", 0),
        pt("dev-b", 120), pt("dev-b", 150, inside=False),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # 乱序补录：b 在 200 又进入到 320 才离开（先提交离开点，再补进入点）
    submit(client, [pt("dev-b", 320, inside=False)])
    submit(client, [pt("dev-b", 200)])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(200)
    assert datetime.fromisoformat(seg["end"]) == dts(320)
    assert seg["duration_s"] == 120.0

    # 再补录更早的点把 b 的第二段在场打断为不足 60s：报告重算，旧结果不保留
    # 补 230 离开与 300 进入：共同片段切成 [200,230)(30s) 与 [300,320)(20s)
    submit(client, [
        pt("dev-b", 230, inside=False),
        pt("dev-b", 300),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []


def test_other_devices_and_other_fences_ignored(client):
    fence_id = create_fence(client)
    other_fence = create_fence(client)
    submit(client, [
        pt("dev-a", 0),
        pt("dev-b", 0), pt("dev-b", 350, inside=False),
        # 旁观者全程在场，不应影响成员集合
        pt("dev-z", 0),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    assert rep["segments"][0]["members"] == ["dev-a", "dev-b"]

    # 另一围栏的任务存在不影响本围栏报告
    create_task(client, other_fence, devices=("dev-a", "dev-z"))
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert rep["segments"][0]["members"] == ["dev-a", "dev-b"]


def test_delete_fence_removes_coattendance_tasks(client):
    fence_id = create_fence(client)
    create_task(client, fence_id)
    assert client.delete(f"/fences/{fence_id}").status_code == 204

    assert client.get(f"/fences/{fence_id}/coattendance-tasks").status_code == 404
    new_id = create_fence(client)
    assert client.get(f"/fences/{new_id}/coattendance-tasks").json() == []
