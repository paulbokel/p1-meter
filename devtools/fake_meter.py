"""Fake HomeWizard P1 meter (API v1) for development without the real meter.

    python devtools/fake_meter.py --port 8080

Serves /api and /api/v1/data with counters that advance in real time, a
daily load curve, and solar export around midday.
"""
import argparse
import json
import math
import random
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

state = {"t": time.time(), "imp1": 10830.0, "imp2": 2948.0, "exp1": 234.0, "exp2": 1000.0, "gas": 2569.0}


def power_now() -> float:
    h = datetime.now().hour + datetime.now().minute / 60
    load = 180 + 400 * max(0, math.sin((h - 6) / 16 * math.pi)) + random.uniform(-40, 40)
    if random.random() < 0.02:
        load += 2000  # kettle
    solar = 2500 * max(0, math.sin((h - 7) / 12 * math.pi))
    return load - solar


def advance():
    now = time.time()
    dt, state["t"] = now - state["t"], now
    p = power_now()
    state["p"] = p
    hour = datetime.now().hour
    tariff = 1 if hour < 7 or hour >= 23 or datetime.now().weekday() >= 5 else 2
    kwh = abs(p) * dt / 3_600_000
    key = ("imp" if p >= 0 else "exp") + str(tariff)
    state[key] += kwh
    state["gas"] += 0.00002 * dt
    state["tariff"] = tariff


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        advance()
        if self.path == "/api":
            body = {"product_type": "HWE-P1", "product_name": "P1 meter (fake)", "serial": "3c39e7aabbcc",
                    "firmware_version": "5.18", "api_version": "v1"}
        elif self.path == "/api/v1/data":
            p = state["p"]
            split = [0.5, 0.3, 0.2]
            v = [230 + random.uniform(-2, 2) for _ in range(3)]
            body = {
                "wifi_ssid": "fake", "wifi_strength": 80, "smr_version": 50, "meter_model": "FAKE 2M550T",
                "unique_id": "FAKE", "active_tariff": state["tariff"],
                "total_power_import_kwh": round(state["imp1"] + state["imp2"], 3),
                "total_power_import_t1_kwh": round(state["imp1"], 3),
                "total_power_import_t2_kwh": round(state["imp2"], 3),
                "total_power_export_kwh": round(state["exp1"] + state["exp2"], 3),
                "total_power_export_t1_kwh": round(state["exp1"], 3),
                "total_power_export_t2_kwh": round(state["exp2"], 3),
                "active_power_w": round(p),
                **{f"active_power_l{i+1}_w": round(p * s) for i, s in enumerate(split)},
                **{f"active_voltage_l{i+1}_v": round(v[i], 1) for i in range(3)},
                **{f"active_current_l{i+1}_a": round(p * s / v[i], 2) for i, s in enumerate(split)},
                **{f"voltage_sag_l{i}_count": 0 for i in (1, 2, 3)},
                **{f"voltage_swell_l{i}_count": 0 for i in (1, 2, 3)},
                "any_power_fail_count": 3, "long_power_fail_count": 1,
                "external": [{"unique_id": "G", "type": "gas_meter", "unit": "m3",
                              "timestamp": int(datetime.now().strftime("%y%m%d%H%M00")),
                              "value": round(state["gas"], 3)}],
            }
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=80)
    a = ap.parse_args()
    advance()
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
