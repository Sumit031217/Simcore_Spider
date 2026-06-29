import csv
import json
import math
import random
import socket
import time
import threading
from io import StringIO
from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Any

from geopy.distance import geodesic
from database import SessionLocal, engine, Base, SimulationRun, AlertLog, DeviceConfigDB, SchemaConfigDB, ScenarioStateDB, ActiveAlertDB, TelemetryLogDB
from sqlalchemy.orm import Session
from fastapi import Depends

try:
    Base.metadata.create_all(bind=engine)
    print("SUCCESS: Connected to PostgreSQL Database.")
except Exception as e:
    print("\nWARNING: Could not connect to PostgreSQL Database.")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

app = FastAPI(title="SIMCORE v2.5 Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ==========================================================
# GLOBAL ENGINE STATE
# ==========================================================
engine_lock = threading.Lock()
engine_state = {
    "is_running": False,
    "should_abort": False,
    "progress": 0,
    "total": 0,
    "logs": [],
    "map_alerts": []
}

# ==========================================================
# PYDANTIC MODELS
# ==========================================================
class DeviceModel(BaseModel):
    id: str
    type: str
    lat: float
    lng: float
    innerRange: float
    outerRange: float
    azimuth: float
    fov: float
    alertCount: int = 0 
    packetChoice: str = "" 
    isPolygon: bool = False
    polygon: Optional[list] = []

class SchemaModel(BaseModel):
    name: str
    separator: str
    totalIndexes: int
    schema_data: list = Field(default=[], alias="schema")

class ScenarioModel(BaseModel):
    name: str
    activeDevices: list
    udpIp: str
    udpPort: int

class ExportRequest(BaseModel):
    scenarioName: str
    devices: List[DeviceModel]
    alerts: List[dict]

def generate_uniform_distance(min_range, max_range):
    return math.sqrt(random.uniform(min_range ** 2, max_range ** 2))

def determine_priority(distance):
    if distance <= 1500: return "HIGH"
    if distance <= 3500: return "MEDIUM"
    return "LOW"

# ==========================================================
# MAGIC DYNAMIC PACKET BUILDER
# ==========================================================
def build_dynamic_packet(alert, device, track_id, schema, separator):
    clean_type = device.type.upper()
    if not schema:
        clean_id = device.id.replace("RADAR_", "").replace("CAM_", "").replace("PIDS_", "")
        if "PIDS" in clean_type:
            packet = [clean_id, 25, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1112, 0, 0, 0, 0, 0, track_id, 0]
            return ",".join(map(str, packet))
        elif "CAM" in clean_type:
            packet = [clean_id, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "Intrusion", 0, 0, 0]
            return ",".join(map(str, packet))
        else: 
            fov_start = (device.azimuth - (device.fov / 2)) % 360
            fov_end = (device.azimuth + (device.fov / 2)) % 360
            packet = [clean_id, 9, round(device.lat, 6), round(device.lng, 6), 0, round(device.azimuth, 2), round(fov_start, 2), round(fov_end, 2), track_id, round(alert["latitude"], 8), round(alert["longitude"], 8), round(alert["distance_m"], 2), round(alert["bearing"], 2), 0, 95, int(time.time()), 0, "", 0, 0, 0]
            return ",".join(map(str, packet))

    packet = []
    sorted_schema = sorted(schema, key=lambda x: x.get('index', 0))
    for field in sorted_schema:
        if field.get('staticValue') and str(field.get('staticValue')).strip() != "":
            packet.append(str(field.get('staticValue')).strip())
            continue

        fname = field.get('name', '').lower()
        dtype = field.get('dataType', '')
        val = 0 
        
        if 'deviceid' in fname or 'sensorid' in fname: val = device.id.replace("RADAR_", "").replace("CAM_", "").replace("PIDS_", "")
        elif 'devicetype' in fname or 'sensortype' in fname: val = 9 if "RADAR" in clean_type else 10 if "CAM" in clean_type else 11
        elif 'devicelat' in fname or ('lat' in fname and 'target' not in fname): val = round(device.lat, 6)
        elif 'devicelong' in fname or 'devicelng' in fname or ('lon' in fname and 'target' not in fname): val = round(device.lng, 6)
        elif 'targetlat' in fname or 'alertlat' in fname: val = round(alert['latitude'], 8)
        elif 'targetlong' in fname or 'alertlong' in fname: val = round(alert['longitude'], 8)
        elif 'range' in fname or 'distance' in fname: val = round(alert['distance_m'], 2)
        elif 'bearing' in fname and 'device' not in fname: val = round(alert['bearing'], 2)
        elif 'trackid' in fname or 'nodeid' in fname: val = track_id
        elif 'time' in fname or 'timestamp' in fname: val = int(time.time())
        elif 'targettype' in fname: val = 0 
        elif 'otherinfo' in fname or 'analyticname' in fname: val = "Intrusion" if alert['priority'] == 'HIGH' else "Motion"
        
        if dtype == 'Integer':
            try: val = int(float(val))
            except: val = 0
        elif dtype == 'Float/Double':
            try: val = float(val)
            except: val = 0.0
        elif dtype == 'String': val = str(val)
        elif dtype == 'Boolean': val = bool(val)
            
        packet.append(str(val))
    return separator.join(packet)


# ==========================================================
# THE HIGH PERFORMANCE ENGINE WORKER 
# ==========================================================
def simulation_worker(scenarioName, udpIp, udpPort, active_devices, env_devices, schemas, minDelay, maxDelay):
    global engine_state
    
    pool = []
    for dev in active_devices:
        count = dev.get('alertCount', 0)
        for _ in range(count):
            pool.append(dev)
            
    random.shuffle(pool)
    total = len(pool)

    with engine_lock:
        engine_state['is_running'] = True
        engine_state['should_abort'] = False
        engine_state['progress'] = 0
        engine_state['total'] = total
        engine_state['logs'] = [{"time": datetime.now().strftime("%H:%M:%S"), "msg": f"SYSTEM: Engaging '{scenarioName}'. UDP Engine Active.", "type": "info"}]
        engine_state['map_alerts'] = []

    if total == 0:
        with engine_lock:
            engine_state['is_running'] = False
        return

    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    all_generated_alerts = [] 
    
    class DummyDev: pass

    for idx, dev_dict in enumerate(pool):
        if engine_state['should_abort']:
            with engine_lock:
                engine_state['logs'].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": "SYSTEM: Transmission Aborted manually.", "type": "error"})
            break

        d_obj = DummyDev()
        d_obj.id = dev_dict.get('id', '')
        d_obj.type = dev_dict.get('type', '')
        d_obj.lat = dev_dict.get('lat', 0.0)
        d_obj.lng = dev_dict.get('lng', 0.0)
        d_obj.innerRange = dev_dict.get('innerRange', 0.0)
        d_obj.outerRange = dev_dict.get('outerRange', 100.0)
        d_obj.azimuth = dev_dict.get('azimuth', 0.0)
        d_obj.fov = dev_dict.get('fov', 360.0)
        d_obj.isPolygon = dev_dict.get('isPolygon', False)
        d_obj.polygon = dev_dict.get('polygon', [])
        d_obj.packetChoice = dev_dict.get('packetChoice', '')

        clean_type = d_obj.type.upper()
        
        if "PIDS" in clean_type and d_obj.isPolygon and d_obj.polygon and len(d_obj.polygon) > 1:
            idx_poly = random.randint(0, len(d_obj.polygon) - 1)
            p1 = d_obj.polygon[idx_poly]; p2 = d_obj.polygon[(idx_poly + 1) % len(d_obj.polygon)]
            fraction = random.uniform(0, 1)
            edge_lat = p1[0] + fraction * (p2[0] - p1[0])
            edge_lng = p1[1] + fraction * (p2[1] - p1[1])
            offset_dist = random.uniform(0, 10)
            offset_bearing = random.uniform(0, 360)
            destination = geodesic(meters=offset_dist).destination((edge_lat, edge_lng), offset_bearing)
            alert_lat = round(destination.latitude, 8); alert_lng = round(destination.longitude, 8)
            distance = round(offset_dist, 2); bearing = round(offset_bearing, 2)
            priority = "HIGH"
        else:
            distance = generate_uniform_distance(d_obj.innerRange, d_obj.outerRange)
            bearing = random.uniform(d_obj.azimuth - (d_obj.fov / 2), d_obj.azimuth + (d_obj.fov / 2)) % 360 if "CAM" in clean_type else random.uniform(0, 360)
            destination = geodesic(meters=distance).destination((d_obj.lat, d_obj.lng), bearing)
            alert_lat = round(destination.latitude, 8); alert_lng = round(destination.longitude, 8)
            priority = determine_priority(distance)

        track_id = idx + 1
        alert_data = {
            "sensor_type": clean_type, "sensor_name": d_obj.id, "alert_id": track_id,
            "priority": priority, "latitude": alert_lat, "longitude": alert_lng,
            "distance_m": round(distance, 2), "bearing": round(bearing, 2), "timestamp": datetime.now(timezone.utc).isoformat()
        }

        sel_schema = next((s['schema'] for s in schemas if s['name'].upper() == d_obj.packetChoice.upper()), None)
        sel_sep = next((s['separator'] for s in schemas if s['name'].upper() == d_obj.packetChoice.upper()), ",")

        packet_string = build_dynamic_packet(alert_data, d_obj, track_id, sel_schema, sel_sep)
        try: udp_socket.sendto(packet_string.encode('utf-8'), (udpIp, udpPort))
        except Exception: pass

        all_generated_alerts.append(alert_data)

        if idx % 15 == 0 or idx == total - 1:
            with engine_lock:
                engine_state['progress'] = idx + 1
                engine_state['map_alerts'].append(alert_data)
                
                # PERFORMANCE FIX: Strict sliding window of latest 1000 items
                if len(engine_state['map_alerts']) > 1000: 
                    engine_state['map_alerts'] = engine_state['map_alerts'][-1000:]
                
                engine_state['logs'].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": f"[{d_obj.id}] -> {packet_string}", "type": "success"})
                if len(engine_state['logs']) > 50:
                    engine_state['logs'] = engine_state['logs'][:50]

        delay = random.uniform(minDelay, maxDelay)
        if delay > 0: time.sleep(delay)

    udp_socket.close()

    if not engine_state['should_abort']:
        with engine_lock:
            engine_state['progress'] = total
            engine_state['logs'].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": f"SYSTEM: Transmission Complete. {total} packets sent.", "type": "info"})
            engine_state['logs'].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": f"DATABASE: Chunking and compiling {total} records into DB...", "type": "info"})

        db = SessionLocal()
        try:
            db_run = SimulationRun(
                scenario_name=scenarioName, total_alerts=total, 
                timestamp=datetime.now(timezone.utc).isoformat(),
                devices_snapshot=json.dumps(active_devices + env_devices) 
            )
            db.add(db_run)
            db.commit()
            db.refresh(db_run)

            chunk_size = 5000
            alert_dicts = []
            for a in all_generated_alerts:
                alert_dicts.append({
                    "run_id": db_run.id, "sensor_type": a["sensor_type"], "sensor_name": a["sensor_name"],
                    "alert_id": a["alert_id"], "priority": a["priority"], "latitude": a["latitude"],
                    "longitude": a["longitude"], "distance_m": a["distance_m"], "bearing": a["bearing"], "timestamp": a["timestamp"]
                })

            for i in range(0, len(alert_dicts), chunk_size):
                db.bulk_insert_mappings(AlertLog, alert_dicts[i:i+chunk_size])
            db.commit()

            with engine_lock:
                engine_state['logs'].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": "DATABASE: Successfully wrote 100% of data to Postgres history.", "type": "success"})

        except Exception as e:
            with engine_lock:
                engine_state['logs'].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": f"DB ERROR: {str(e)}", "type": "error"})
        finally: db.close()

    with engine_lock:
        engine_state['is_running'] = False


@app.post("/api/engine/start")
async def api_engine_start(payload: dict, background_tasks: BackgroundTasks):
    global engine_state
    with engine_lock:
        if engine_state["is_running"]: return {"status": "error", "message": "Engine is already running."}
    background_tasks.add_task(simulation_worker, payload["scenarioName"], payload["udpIp"], payload["udpPort"], payload["activeDevices"], payload["environmentDevices"], payload["sensorSchemas"], payload["alertConfig"]["minDelaySec"], payload["alertConfig"]["maxDelaySec"])
    return {"status": "success"}

@app.get("/api/engine/status")
def api_engine_status():
    with engine_lock:
        return {
            "is_running": engine_state["is_running"], "progress": engine_state["progress"],
            "total": engine_state["total"], "logs": engine_state["logs"], "map_alerts": engine_state["map_alerts"]
        }

@app.post("/api/engine/stop")
def api_engine_stop():
    with engine_lock: engine_state["should_abort"] = True
    return {"status": "success"}

@app.get("/api/state/alerts")
def get_active_alerts(db: Session = Depends(get_db)):
    # Pull the entire 100k run out of the history DB so React holds it in memory
    last_run = db.query(SimulationRun).order_by(SimulationRun.id.desc()).first()
    if last_run:
        return [{
            "sensor_type": a.sensor_type, "sensor_name": a.sensor_name, "alert_id": a.alert_id,
            "priority": a.priority, "latitude": a.latitude, "longitude": a.longitude,
            "distance_m": a.distance_m, "bearing": a.bearing, "timestamp": a.timestamp
        } for a in last_run.alerts]
    return []

@app.post("/api/export")
async def generate_exports(payload: ExportRequest):
    csv_io = StringIO()
    writer = csv.writer(csv_io)
    writer.writerow(["sensor_type", "sensor_name", "alert_id", "priority", "latitude", "longitude", "distance_m", "bearing", "timestamp"])
    for alert in payload.alerts: writer.writerow([alert["sensor_type"], alert["sensor_name"], alert["alert_id"], alert["priority"], alert["latitude"], alert["longitude"], alert["distance_m"], alert["bearing"], alert["timestamp"]])
    
    kml = f'<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n    <name>{payload.scenarioName} Report</name>\n    <Style id="radarStyle"><IconStyle><color>ff0000ff</color><scale>1.4</scale></IconStyle></Style>\n    <Style id="cameraStyle"><IconStyle><color>ffff0000</color><scale>1.4</scale></IconStyle></Style>\n    <Style id="radarHighStyle"><IconStyle><color>ff0000ff</color><scale>1.2</scale></IconStyle></Style>\n    <Style id="radarMediumStyle"><IconStyle><color>ff00ffff</color><scale>1.2</scale></IconStyle></Style>\n    <Style id="radarLowStyle"><IconStyle><color>ff00ff00</color><scale>1.2</scale></IconStyle></Style>\n    <Style id="cameraHighStyle"><IconStyle><color>ffffffff</color><scale>1.2</scale></IconStyle></Style>\n    <Style id="cameraMediumStyle"><IconStyle><color>ffffffff</color><scale>1.2</scale></IconStyle></Style>\n    <Style id="cameraLowStyle"><IconStyle><color>ffffffff</color><scale>1.2</scale></IconStyle></Style>\n    <Style id="pidsAlertStyle"><IconStyle><color>ffffff00</color><scale>1.3</scale></IconStyle></Style>\n    <Style id="envStyle"><IconStyle><color>ff00ff00</color><scale>1.0</scale></IconStyle></Style>\n'
    for dev in payload.devices:
        clean_type = dev.type.upper()
        if "ENV" in clean_type: 
            kml += f'<Placemark><name>{dev.id}</name><styleUrl>#envStyle</styleUrl><Point><coordinates>{dev.lng},{dev.lat},0</coordinates></Point></Placemark>'
        elif dev.isPolygon and dev.polygon:
            perimeter_coords = " ".join([f"{pt[1]},{pt[0]},0" for pt in dev.polygon]) + f" {dev.polygon[0][1]},{dev.polygon[0][0]},0"
            kml += f'<Placemark><name>{dev.id} Boundary</name><Style><LineStyle><color>ff0000ff</color><width>3</width></LineStyle><PolyStyle><color>440000ff</color></PolyStyle></Style><Polygon><outerBoundaryIs><LinearRing><coordinates>{perimeter_coords}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>'
        else:
            style = "#radarStyle" if "RADAR" in clean_type else "#cameraStyle"
            kml += f'<Placemark><name>{dev.id}</name><styleUrl>{style}</styleUrl><Point><coordinates>{dev.lng},{dev.lat},0</coordinates></Point></Placemark>'
            if "CAM" in clean_type:
                start_bearing, end_bearing = (dev.azimuth - (dev.fov / 2)) % 360, (dev.azimuth + (dev.fov / 2)) % 360
                arc_points = []
                angle = start_bearing
                while True:
                    pt = geodesic(meters=dev.outerRange).destination((dev.lat, dev.lng), angle)
                    arc_points.append(f"{pt.longitude},{pt.latitude},0")
                    angle = (angle + 2) % 360
                    if abs((angle - end_bearing + 360) % 360) < 2: break
                kml += f'<Placemark><name>{dev.id} FOV</name><Style><LineStyle><color>66ff0000</color><width>1</width></LineStyle><PolyStyle><color>2200ff00</color></PolyStyle></Style><Polygon><outerBoundaryIs><LinearRing><coordinates>{dev.lng},{dev.lat},0 {" ".join(arc_points)} {dev.lng},{dev.lat},0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>'
            elif "RADAR" in clean_type:
                outer_pts, inner_pts = [], []
                for angle in range(361):
                    opt = geodesic(meters=dev.outerRange).destination((dev.lat, dev.lng), angle)
                    ipt = geodesic(meters=dev.innerRange).destination((dev.lat, dev.lng), angle)
                    outer_pts.append(f"{opt.longitude},{opt.latitude},0")
                    inner_pts.append(f"{ipt.longitude},{ipt.latitude},0")
                kml += f'<Placemark><name>{dev.id} Boundary</name><LineString><coordinates>{" ".join(outer_pts)}</coordinates></LineString></Placemark><Placemark><name>{dev.id} Exclusion</name><LineString><coordinates>{" ".join(inner_pts)}</coordinates></LineString></Placemark>'

    for alert in payload.alerts:
        clean_type = alert["sensor_type"].upper()
        if "RADAR" in clean_type: style = "#radarHighStyle" if alert["priority"] == "HIGH" else "#radarMediumStyle" if alert["priority"] == "MEDIUM" else "#radarLowStyle"
        elif "PIDS" in clean_type: style = "#pidsAlertStyle"
        else: style = "#cameraHighStyle" if alert["priority"] == "HIGH" else "#cameraMediumStyle" if alert["priority"] == "MEDIUM" else "#cameraLowStyle"
        kml += f'<Placemark><name>{alert["sensor_name"]}_{alert["alert_id"]}</name><description>Priority: {alert["priority"]}\nDistance: {alert["distance_m"]}m\nTimestamp: {alert["timestamp"]}</description><styleUrl>{style}</styleUrl><Point><coordinates>{alert["longitude"]},{alert["latitude"]},0</coordinates></Point></Placemark>'
    kml += "\n</Document>\n</kml>"
    return {"csv_content": csv_io.getvalue(), "kml_content": kml}

@app.get("/api/runs")
def get_all_runs(db: Session = Depends(get_db)):
    runs = db.query(SimulationRun).order_by(SimulationRun.id.desc()).all()
    result = []
    for r in runs:
        alerts = [{
            "sensor_type": a.sensor_type, "sensor_name": a.sensor_name, "alert_id": a.alert_id,
            "priority": a.priority, "latitude": a.latitude, "longitude": a.longitude,
            "distance_m": a.distance_m, "bearing": a.bearing, "timestamp": a.timestamp
        } for a in r.alerts]
        result.append({
            "id": r.id,
            "scenarioName": r.scenario_name,
            "alertsGenerated": r.total_alerts,
            "timestamp": r.timestamp,
            "devices": json.loads(r.devices_snapshot) if r.devices_snapshot else [],
            "alerts": alerts
        })
    return result

# ==========================================================
# DEVICE & SCHEMA CONFIG PERSISTENCE
# ==========================================================
@app.get("/api/config/devices")
def get_saved_devices(db: Session = Depends(get_db)):
    devices = db.query(DeviceConfigDB).all()
    return [{"id": d.id, "type": d.type, "lat": d.lat, "lng": d.lng, "innerRange": d.innerRange, "outerRange": d.outerRange, "azimuth": d.azimuth, "fov": d.fov, "alertCount": d.alertCount, "packetChoice": d.packetChoice, "isPolygon": d.isPolygon, "polygon": json.loads(d.polygon) if d.polygon else []} for d in devices]

@app.post("/api/config/devices")
def save_devices(payload: List[DeviceModel], db: Session = Depends(get_db)):
    for dev in payload:
        try:
            db_dev = db.query(DeviceConfigDB).filter(DeviceConfigDB.id == dev.id).first()
            poly_str = json.dumps(dev.polygon) if dev.polygon else "[]"
            if db_dev:
                db_dev.type = str(dev.type); db_dev.lat = float(dev.lat); db_dev.lng = float(dev.lng); db_dev.innerRange = float(dev.innerRange); db_dev.outerRange = float(dev.outerRange); db_dev.azimuth = float(dev.azimuth); db_dev.fov = float(dev.fov); db_dev.alertCount = int(dev.alertCount); db_dev.packetChoice = str(dev.packetChoice); db_dev.isPolygon = bool(dev.isPolygon); db_dev.polygon = poly_str
            else:
                new_dev = DeviceConfigDB(id=str(dev.id), type=str(dev.type), lat=float(dev.lat), lng=float(dev.lng), innerRange=float(dev.innerRange), outerRange=float(dev.outerRange), azimuth=float(dev.azimuth), fov=float(dev.fov), alertCount=int(dev.alertCount), packetChoice=str(dev.packetChoice), isPolygon=bool(dev.isPolygon), polygon=poly_str)
                db.add(new_dev)
            db.commit()
        except Exception: db.rollback()
    return {"status": "success"}

@app.delete("/api/config/devices/{device_id}")
def delete_device(device_id: str, db: Session = Depends(get_db)):
    db.query(DeviceConfigDB).filter(DeviceConfigDB.id == device_id).delete()
    db.commit()
    return {"status": "success"}

@app.get("/api/config/schemas")
def get_saved_schemas(db: Session = Depends(get_db)):
    schemas = db.query(SchemaConfigDB).all()
    return [{"name": s.name, "separator": s.separator, "totalIndexes": s.totalIndexes, "schema": json.loads(s.schema_data) if s.schema_data else []} for s in schemas]

@app.post("/api/config/schemas")
def save_schemas(payload: List[SchemaModel], db: Session = Depends(get_db)):
    for s in payload:
        try:
            db_schema = db.query(SchemaConfigDB).filter(SchemaConfigDB.name == s.name).first()
            schema_str = json.dumps(s.schema_data) if s.schema_data else "[]"
            if db_schema:
                db_schema.separator = str(s.separator); db_schema.totalIndexes = int(s.totalIndexes); db_schema.schema_data = schema_str
            else:
                new_schema = SchemaConfigDB(name=str(s.name), separator=str(s.separator), totalIndexes=int(s.totalIndexes), schema_data=schema_str)
                db.add(new_schema)
            db.commit()
        except Exception: db.rollback()
    return {"status": "success"}

@app.delete("/api/config/schemas/{schema_name}")
def delete_schema(schema_name: str, db: Session = Depends(get_db)):
    db.query(SchemaConfigDB).filter(SchemaConfigDB.name == schema_name).delete()
    db.commit()
    return {"status": "success"}

@app.get("/api/state/scenario")
def get_scenario_state(db: Session = Depends(get_db)):
    s = db.query(ScenarioStateDB).filter(ScenarioStateDB.id == "current").first()
    if s: return { "name": s.name, "activeDevices": json.loads(s.activeDevices), "udpIp": s.udpIp, "udpPort": s.udpPort }
    return { "name": "Operation Alpha", "activeDevices": [], "udpIp": "127.0.0.1", "udpPort": 5005 }

@app.post("/api/state/scenario")
def save_scenario_state(payload: ScenarioModel, db: Session = Depends(get_db)):
    s = db.query(ScenarioStateDB).filter(ScenarioStateDB.id == "current").first()
    dev_str = json.dumps(payload.activeDevices)
    if s:
        s.name = payload.name; s.activeDevices = dev_str; s.udpIp = payload.udpIp; s.udpPort = payload.udpPort
    else:
        new_s = ScenarioStateDB(id="current", name=payload.name, activeDevices=dev_str, udpIp=payload.udpIp, udpPort=payload.udpPort)
        db.add(new_s)
    db.commit()
    return {"status": "success"}