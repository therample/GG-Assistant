#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoGuessr Assistant — Steam Edition v3
═══════════════════════════════════════════════════════════════════
Fix: Connect to BROWSER-level WS endpoint, auto-attach to ALL pages
via Target.setAutoAttach with flatten=true. This catches every
target including the actual game window.
"""

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
import re
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import math

def _ensure_deps():
    required = {
        'flask': 'flask',
        'flask_cors': 'flask-cors',
        'websocket': 'websocket-client',
        'requests': 'requests',
    }
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        print(f"[*] Installing: {', '.join(missing)}")
        subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + missing)

_ensure_deps()

import requests
import websocket
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("gg")


# ═════════════════════════════════════════════════════════════════
# CDP BROWSER-LEVEL CONNECTION (flat sessions)
# ═════════════════════════════════════════════════════════════════
class CDPConnection:
    """
    Connects to the BROWSER-level CDP endpoint.
    Uses Target.setAutoAttach with flatten=true to automatically
    attach to ALL pages/iframes/webviews and receive their events
    through a single WebSocket connection.
    """

    def __init__(self, debug_port: int):
        self.port = debug_port
        self.ws = None
        self.connected = False
        self.browser_ws_url = None

        self._msg_id = 0
        self._id_lock = threading.Lock()
        self._ws_lock = threading.Lock()
        self._pending = {}  # msg_id -> (event, holder)

        self.sessions = {}  # session_id -> {targetInfo, injected}
        self._session_lock = threading.Lock()

        # Shared state
        self.location_data = {
            "location": None,
            "panoId": None,
            "countryCode": None,
            "round": None,
            "timestamp": None,
            "source": None,
        }
        self.round_history = []
        self._seen_urls = set()
        self._log_lines = []

    def _log(self, msg):
        self._log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(self._log_lines) > 100:
            self._log_lines.pop(0)
        log.info(msg)

    # ─── Discovery ───────────────────────────────────────────
    def get_version(self) -> dict:
        try:
            r = requests.get(f"http://localhost:{self.port}/json/version", timeout=3)
            return r.json()
        except:
            return {}

    def get_targets(self) -> list:
        try:
            r = requests.get(f"http://localhost:{self.port}/json/list", timeout=3)
            if r.status_code == 200:
                return r.json()
            # fallback
            r = requests.get(f"http://localhost:{self.port}/json", timeout=3)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return []

    # ─── Connection ──────────────────────────────────────────
    def connect(self) -> bool:
        version = self.get_version()
        self.browser_ws_url = version.get("webSocketDebuggerUrl")

        if not self.browser_ws_url:
            self._log("ERROR: No browser WS URL. Make sure --remote-debugging-port is set")
            return False

        self._log(f"Browser WS: {self.browser_ws_url}")

        # Also log all targets for debugging
        targets = self.get_targets()
        for t in targets:
            self._log(f"  Target: [{t.get('type','?')}] {t.get('title','?')[:50]} — {t.get('url','?')[:60]}")

        try:
            self.ws = websocket.WebSocketApp(
                self.browser_ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            threading.Thread(target=self.ws.run_forever, daemon=True).start()
            time.sleep(2)

            if self.connected:
                return True
            return False

        except Exception as e:
            self._log(f"WS connect error: {e}")
            return False

    def _on_open(self, ws):
        self.connected = True
        self._log("✓ Browser WS connected")

        # Discover all targets
        self._send("Target.setDiscoverTargets", {"discover": True})

        # Auto-attach to ALL targets (pages, iframes, webviews)
        # flatten=true → all events come through this single WS
        self._send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })

        # Also try to attach to existing targets manually
        self._attach_existing_targets()

    def _attach_existing_targets(self):
        """Manually attach to existing page targets as backup."""
        targets = self.get_targets()
        for t in targets:
            if t.get("type") in ("page", "webview", "iframe"):
                tid = t.get("id")
                if tid:
                    self._send("Target.attachToTarget", {
                        "targetId": tid,
                        "flatten": True,
                    })

    def _on_error(self, ws, error):
        self.connected = False
        log.error(f"WS error: {error}")

    def _on_close(self, ws, code, msg):
        self.connected = False
        log.warning(f"WS closed: {code} {msg}")

    # ─── Send Commands ───────────────────────────────────────
    def _send(self, method: str, params: dict = None, session_id: str = None) -> int:
        if not self.ws or not self.connected:
            return -1

        with self._id_lock:
            self._msg_id += 1
            mid = self._msg_id

        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id

        try:
            with self._ws_lock:
                self.ws.send(json.dumps(msg))
        except Exception as e:
            log.error(f"Send error: {e}")
        return mid

    def _send_wait(self, method, params=None, session_id=None, timeout=5):
        """Send and wait for response."""
        if not self.ws or not self.connected:
            return None

        with self._id_lock:
            self._msg_id += 1
            mid = self._msg_id

        event = threading.Event()
        holder = {"data": None}
        self._pending[mid] = (event, holder)

        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id

        try:
            with self._ws_lock:
                self.ws.send(json.dumps(msg))
        except:
            self._pending.pop(mid, None)
            return None

        if event.wait(timeout=timeout):
            self._pending.pop(mid, None)
            return holder["data"]
        self._pending.pop(mid, None)
        return None

    def evaluate_js(self, expression, session_id=None, await_promise=True):
        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }
        resp = self._send_wait("Runtime.evaluate", params, session_id, timeout=10)
        if resp and "result" in resp:
            return resp["result"].get("result", {}).get("value")
        return None

        # ─── 5K MARKER CONTROL ───────────────────────────────────
    def eval_all_sessions(self, js, await_promise=False):
        """Выполнить JS во всех подключённых страницах игры."""
        results = []
        with self._session_lock:
            sids = list(self.sessions.keys())
        for sid in sids:
            try:
                r = self.evaluate_js(js, session_id=sid, await_promise=await_promise)
                if r is not None:
                    results.append(r)
            except Exception:
                pass
        return results

    def place_5k(self, lat, lng):
        # внедряем модуль (идемпотентно — работает и без перезагрузки игры)
        self.eval_all_sessions(FIVEK_SCRIPT)
        js = ("window.__ggPlace5K ? window.__ggPlace5K(%s, %s) "
              ": {ok: false, reason: 'not-installed'}"
              % (repr(float(lat)), repr(float(lng))))
        results = self.eval_all_sessions(js)
        placed = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
        reasons = [r.get("reason") for r in results
                   if isinstance(r, dict) and not r.get("ok") and r.get("reason")]
        return placed, reasons

    def clear_5k(self):
        self.eval_all_sessions("window.__ggClear5K ? window.__ggClear5K() : null")

    # ─── Message Router ──────────────────────────────────────
    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except:
            return

        msg_id = data.get("id")
        method = data.get("method", "")
        session_id = data.get("sessionId")

        # Response to our command
        if msg_id and msg_id in self._pending:
            event, holder = self._pending[msg_id]
            holder["data"] = data
            event.set()
            return

        # ── Target attached to us ──
        if method == "Target.attachedToTarget":
            self._handle_target_attached(data)
            return

        # ── Target detached ──
        if method == "Target.detachedFromTarget":
            sid = data.get("params", {}).get("sessionId")
            if sid:
                with self._session_lock:
                    self.sessions.pop(sid, None)
            return

        # ── Target created/destroyed ──
        if method == "Target.targetCreated":
            info = data.get("params", {}).get("targetInfo", {})
            self._log(f"  [New Target] [{info.get('type')}] {info.get('title','')[:50]}")
            return

        # ── Events from sessions (pages) ──
        if session_id:
            if method.startswith("Network."):
                self._handle_network(data, session_id)
            elif method == "Runtime.consoleAPICalled":
                self._handle_console(data, session_id)
            elif method == "Page.frameNavigated":
                frame = data.get("params", {}).get("frame", {})
                if frame.get("parentId") is None:
                    url = frame.get("url", "")
                    if url and url != "about:blank":
                        self._log(f"  [Nav] {url[:80]}")
                        # Re-inject after navigation
                        threading.Timer(2.0, self._inject_session, args=[session_id]).start()

    # ─── Target Attached Handler ─────────────────────────────
    def _handle_target_attached(self, data):
        params = data.get("params", {})
        target_info = params.get("targetInfo", {})
        session_id = params.get("sessionId")

        if not session_id:
            return

        ttype = target_info.get("type", "?")
        ttitle = target_info.get("title", "?")
        turl = target_info.get("url", "?")

        with self._session_lock:
            already = session_id in self.sessions
            self.sessions[session_id] = {
                "targetInfo": target_info,
                "injected": False,
            }

        if not already:
            self._log(f"✓ Attached: [{ttype}] {ttitle[:40]} — {turl[:60]}")

        # Enable domains on this session
        self._send("Network.enable", {
            "maxTotalBufferSize": 100000000,
            "maxResourceBufferSize": 50000000,
        }, session_id)

        self._send("Runtime.enable", {}, session_id)
        self._send("Page.enable", {}, session_id)

        # Inject our script
        self._inject_session(session_id)

    def _inject_session(self, session_id):
        """Inject JS into a specific session."""
        with self._session_lock:
            if session_id in self.sessions and self.sessions[session_id].get("injected"):
                return
            if session_id in self.sessions:
                self.sessions[session_id]["injected"] = True

        self._send("Runtime.evaluate", {
            "expression": INJECTION_SCRIPT,
            "returnByValue": True,
        }, session_id)

        # Also persistent injection for future navigations
        self._send("Page.addScriptToEvaluateOnNewDocument", {
            "source": INJECTION_SCRIPT,
            "runImmediately": True,
        }, session_id)

    # ─── Console Handler ─────────────────────────────────────
    def _handle_console(self, data, session_id):
        params = data.get("params", {})
        args = params.get("args", [])
        ctype = params.get("type", "")

        for arg in args:
            val = arg.get("value", "")
            if isinstance(val, str):
                if val.startswith("__GG_UPDATE__"):
                    try:
                        payload = json.loads(val[len("__GG_UPDATE__"):])
                        self._update_location(payload)
                    except:
                        pass
                elif val.startswith("[GG]") and ctype in ("log", "info"):
                    self._log(f"    JS: {val}")

    # ─── Network Handlers ────────────────────────────────────
    def _handle_network(self, data, session_id):
        method = data.get("method", "")
        params = data.get("params", {})

        if method == "Network.requestWillBeSent":
            url = params.get("request", {}).get("url", "")
            if url and url not in ("about:blank", ""):
                self._track_url(url)

        elif method == "Network.responseReceived":
            response = params.get("response", {})
            url = response.get("url", "")
            status = response.get("status", 0)
            mime = response.get("mimeType", "")
            req_id = params.get("requestId", "")

            if url and status < 400:
                self._track_url(url)

                # Fetch body for interesting URLs
                if self._is_interesting(url, mime):
                    threading.Thread(
                        target=self._fetch_body,
                        args=(req_id, url, session_id),
                        daemon=True
                    ).start()

    def _is_interesting(self, url, mime):
        u = url.lower()
        # GeoGuessr API
        if "geoguessr" in u and "/api/" in u:
            return True
        # JSON responses from Google
        if "json" in (mime or "").lower():
            if any(x in u for x in ["googleapis", "geophotoservice", "cbk0", "cbk1"]):
                return True
        # Street View metadata
        if any(x in u for x in ["geophotoservice", "cbk0.google", "cbk1.google"]):
            return True
        return False

    def _track_url(self, url):
        # Always track Street View panoIds
        u = url.lower()
        if "panoid=" in u or "pano_id=" in u:
            self._extract_pano(url)

        # Track GeoGuessr API URLs
        if "geoguessr" in u and "/api/" in u:
            if url not in self._seen_urls:
                self._seen_urls.add(url)
                self._log(f"🌐 API: {url[:120]}")

        # Track Google Maps/Street View URLs
        if any(x in u for x in ["streetviewpixels", "geophotoservice", "cbk0", "cbk1"]):
            if url not in self._seen_urls:
                self._seen_urls.add(url)
                self._log(f"📸 SV: {url[:150]}")

    def _extract_pano(self, url):
        pano_id = None
        match = re.search(r'[?&]panoid=([^&]+)', url, re.IGNORECASE)
        if not match:
            match = re.search(r'[?&]pano_id=([^&]+)', url, re.IGNORECASE)
        if match:
            pano_id = match.group(1)

        if pano_id and pano_id != self.location_data.get("panoId"):
            self._log(f"🎯 panoId: {pano_id[:40]}")
            self.location_data["panoId"] = pano_id

            # Try conversion via ALL sessions
            threading.Thread(
                target=self._pano_lookup,
                args=(pano_id,),
                daemon=True
            ).start()

    def _pano_lookup(self, pano_id):
        """Try to get coords from panoId using any available session."""
        with self._session_lock:
            sessions = list(self.sessions.keys())

        for sid in sessions:
            js = f"""
            (async function() {{
                try {{
                    if (!window.google || !window.google.maps) return null;
                    if (!google.maps.StreetViewService) return null;
                    var s = new google.maps.StreetViewService();
                    return await new Promise(function(resolve) {{
                        s.getPanorama({{pano: "{pano_id}"}}, function(d, st) {{
                            if (st === 'OK' && d && d.location && d.location.latLng) {{
                                resolve(JSON.stringify({{
                                    lat: d.location.latLng.lat(),
                                    lng: d.location.latLng.lng()
                                }}));
                            }} else resolve(null);
                        }});
                    }});
                }} catch(e) {{ return null; }}
            }})()
            """
            result = self.evaluate_js(js, session_id=sid, await_promise=True)
            if result and isinstance(result, str):
                try:
                    coords = json.loads(result)
                    if "lat" in coords:
                        self._log(f"📍 panoId→{coords['lat']:.5f},{coords['lng']:.5f}")
                        self._update_location({
                            "location": coords,
                            "panoId": pano_id,
                            "source": "panoId-lookup"
                        })
                        return
                except:
                    pass

    def _fetch_body(self, req_id, url, session_id):
        """Get response body for a network request."""
        try:
            resp = self._send_wait(
                "Network.getResponseBody",
                {"requestId": req_id},
                session_id,
                timeout=3
            )
            if resp and "result" in resp:
                body = resp["result"].get("body", "")
                if body:
                    self._scan_body(body, url)
        except:
            pass

    def _scan_body(self, body, url):
        """Search body for coordinates."""
        try:
            data = json.loads(body)
            self._scan_json(data, url)
        except:
            pass
        self._scan_text(body, url)

    def _scan_json(self, data, url, depth=0):
        if depth > 10 or data is None:
            return

        if isinstance(data, dict):
            lat = data.get("lat") or data.get("latitude")
            lng = data.get("lng") or data.get("lon") or data.get("longitude")

            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                if self._valid(lat, lng):
                    self._update_location({
                        "location": {"lat": lat, "lng": lng},
                        "source": f"api:{url[:50]}"
                    })
                    return

            cc = data.get("countryCode")
            if isinstance(cc, str) and len(cc) == 2:
                self.location_data["countryCode"] = cc

            if isinstance(data.get("round"), int):
                self.location_data["round"] = data["round"]

            if data.get("panoId") or data.get("panoid"):
                pid = data.get("panoId") or data.get("panoid")
                if pid != self.location_data.get("panoId"):
                    self.location_data["panoId"] = pid
                    self._log(f"🎯 panoId (API): {str(pid)[:40]}")
                    self._pano_lookup(pid)

            for v in data.values():
                if isinstance(v, (dict, list)):
                    self._scan_json(v, url, depth + 1)

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._scan_json(item, url, depth + 1)

    def _scan_text(self, text, url):
        if not text or len(text) > 500000:
            return

        patterns = [
            r'"lat"\s*:\s*(-?\d+\.?\d*)\s*[,}].*?"(?:lng|lon)"\s*:\s*(-?\d+\.?\d*)',
            r'"latitude"\s*:\s*(-?\d+\.?\d*).*?"longitude"\s*:\s*(-?\d+\.?\d*)',
        ]
        for pat in patterns:
            matches = re.findall(pat, text[:100000], re.IGNORECASE)
            for lat_s, lng_s in matches:
                try:
                    lat, lng = float(lat_s), float(lng_s)
                    if self._valid(lat, lng):
                        self._update_location({
                            "location": {"lat": lat, "lng": lng},
                            "source": f"text:{url[:50]}"
                        })
                        return
                except:
                    pass

    def _valid(self, lat, lng):
        return (
            isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and
            -90 <= lat <= 90 and -180 <= lng <= 180 and
            (abs(lat) > 0.1 or abs(lng) > 0.1)
        )

    # ─── Update Location ─────────────────────────────────────
    def _update_location(self, payload):
        location = payload.get("location")
        if not location or not isinstance(location, dict):
            return
        lat, lng = location.get("lat"), location.get("lng")
        if not self._valid(lat, lng):
            return

        old = self.location_data.get("location")
        if old and abs(old.get("lat", 0) - lat) < 0.001 and abs(old.get("lng", 0) - lng) < 0.001:
            return

        self.location_data.update(payload)
        self.location_data["timestamp"] = time.time()
        self._log(f"═══ 📍 {lat:.6f}, {lng:.6f} ═══ (source: {payload.get('source','?')})")

        # Add to history
        self.round_history.append({
            "lat": lat, "lng": lng,
            "round": len(self.round_history) + 1,
            "timestamp": time.time(),
        })
                # 5K: новый раунд (большой прыжок) → убрать старый маркер из игры
        if old:
            try:
                d = math.hypot(
                    (old.get("lat", 0) - lat) * 111000,
                    (old.get("lng", 0) - lng) * 111000 * math.cos(math.radians(lat))
                )
                if d > 500:
                    threading.Thread(target=self.clear_5k, daemon=True).start()
            except Exception:
                pass

    # ─── Polling ─────────────────────────────────────────────
    def poll(self):
        """Poll all sessions for state + try direct JS access."""
        if not self.connected:
            return

        with self._session_lock:
            sessions = list(self.sessions.keys())

        for sid in sessions:
            # Check injected state
            result = self.evaluate_js(
                "JSON.stringify(window.__gg_assistant || {})",
                session_id=sid, await_promise=False
            )
            if result and isinstance(result, str):
                try:
                    d = json.loads(result)
                    if d.get("location"):
                        self._update_location(d)
                except:
                    pass

            # Try direct panorama access
            js = """
            (function(){
                try{
                    if(!window.google||!window.google.maps)return null;
                    if(!window.__gg_panoramas)return null;
                    for(var i=window.__gg_panoramas.length-1;i>=0;i--){
                        var p=window.__gg_panoramas[i];
                        try{
                            var pos=p.getPosition();
                            if(pos)return JSON.stringify({lat:pos.lat(),lng:pos.lng()});
                        }catch(e){}
                    }
                }catch(e){}
                return null;
            })()
            """
            result = self.evaluate_js(js, session_id=sid, await_promise=False)
            if result and isinstance(result, str):
                try:
                    coords = json.loads(result)
                    if "lat" in coords:
                        self._update_location({
                            "location": coords, "source": "js:panorama"
                        })
                except:
                    pass

    def disconnect(self):
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        self.connected = False


# ═════════════════════════════════════════════════════════════════
# INJECTION SCRIPT
# ═════════════════════════════════════════════════════════════════
INJECTION_SCRIPT = r"""
(function() {
    'use strict';
    if (window.__gg_injected) return;
    window.__gg_injected = true;

    var state = {
        location: null,
        panoId: null,
        countryCode: null,
        round: null,
        timestamp: null,
        source: null
    };
    window.__gg_assistant = state;
    window.__gg_panoramas = [];

    function report(data) {
        Object.assign(state, data);
        state.timestamp = Date.now();
        console.log('__GG_UPDATE__' + JSON.stringify(state));
    }

    function reportLoc(lat, lng, src) {
        if (typeof lat !== 'number' || typeof lng !== 'number') return;
        if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return;
        if (lat === 0 && lng === 0) return;
        if (state.location && Math.abs(state.location.lat - lat) < 0.0001 &&
            Math.abs(state.location.lng - lng) < 0.0001) return;
        report({location: {lat: lat, lng: lng}, source: src});
    }

    // ─── FETCH HOOK ───
    var origFetch = window.fetch;
    window.fetch = async function() {
        var resp = await origFetch.apply(this, arguments);
        try {
            var url = typeof arguments[0] === 'string' ? arguments[0] :
                     (arguments[0] && arguments[0].url) || '';
            if (url.includes('/api/') || url.includes('geoguessr')) {
                var clone = resp.clone();
                var text = await clone.text();
                try {
                    var data = JSON.parse(text);
                    scanData(data, 'fetch');
                } catch(e) {}
            }
        } catch(e) {}
        return resp;
    };

    // ─── XHR HOOK ───
    var origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        var xhr = this;
        this.addEventListener('load', function() {
            try {
                if ((url||'').includes('/api/') || (url||'').includes('geoguessr')) {
                    var data = JSON.parse(xhr.responseText);
                    scanData(data, 'xhr');
                }
            } catch(e) {}
        });
        return origOpen.apply(this, arguments);
    };

    function scanData(data, src) {
        if (!data || typeof data !== 'object') return;
        if (data.location && typeof data.location.lat === 'number') {
            reportLoc(data.location.lat, data.location.lng, src);
        }
        if (Array.isArray(data.rounds)) {
            var last = data.rounds[data.rounds.length-1];
            if (last) {
                if (last.location) reportLoc(last.location.lat, last.location.lng, src+':rounds');
                if (typeof last.lat === 'number') reportLoc(last.lat, last.lng, src+':rounds');
                if (last.countryCode) state.countryCode = last.countryCode;
                if (last.panoId || last.panoid) report({panoId: last.panoId || last.panoid});
            }
        }
        if (typeof data.countryCode === 'string' && data.countryCode.length === 2) {
            state.countryCode = data.countryCode;
        }
        if (typeof data.round === 'number') state.round = data.round;
        if (data.panoId) report({panoId: data.panoId});
    }

    // ─── GOOGLE MAPS HOOKS ───
    function hookGM() {
        if (!window.google || !window.google.maps) {
            setTimeout(hookGM, 200);
            return;
        }
        try {
            if (google.maps.StreetViewPanorama) {
                var proto = google.maps.StreetViewPanorama.prototype;

                var origSP = proto.setPosition;
                proto.setPosition = function(latLng) {
                    try {
                        var lat = latLng && typeof latLng.lat === 'function' ? latLng.lat() : (latLng && latLng.lat);
                        var lng = latLng && typeof latLng.lng === 'function' ? latLng.lng() : (latLng && latLng.lng);
                        if (typeof lat === 'number') reportLoc(lat, lng, 'gm:setPosition');
                    } catch(e) {}
                    return origSP.apply(this, arguments);
                };

                var origSetPano = proto.setPano;
                proto.setPano = function(pid) {
                    if (pid && pid !== state.panoId) {
                        report({panoId: pid, source: 'gm:setPano'});
                    }
                    return origSetPano.apply(this, arguments);
                };
            }

            if (google.maps.StreetViewService) {
                var origGP = google.maps.StreetViewService.prototype.getPanorama;
                google.maps.StreetViewService.prototype.getPanorama = function(req, cb) {
                    var wrapped = function(data, status) {
                        try {
                            if (data && data.location && data.location.latLng) {
                                var ll = data.location.latLng;
                                var lat = typeof ll.lat === 'function' ? ll.lat() : ll.lat;
                                var lng = typeof ll.lng === 'function' ? ll.lng() : ll.lng;
                                if (typeof lat === 'number') reportLoc(lat, lng, 'gm:service');
                            }
                            if (data && data.location && data.location.pano) {
                                report({panoId: data.location.pano});
                            }
                        } catch(e) {}
                        if (cb) cb(data, status);
                    };
                    return origGP.call(this, req, wrapped);
                };
            }
            console.log('[GG] GM hooks OK');
        } catch(e) {
            console.log('[GG] GM hook err: ' + e.message);
        }
    }

    // ─── IMAGE SRC INTERCEPT (catch SV tile URLs) ───
    try {
        var origImgSrc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
        if (origImgSrc && origImgSrc.set) {
            Object.defineProperty(HTMLImageElement.prototype, 'src', {
                set: function(val) {
                    if (typeof val === 'string' && val.includes('panoid=')) {
                        var m = val.match(/panoid=([^&]+)/);
                        if (m && m[1] !== state.panoId) {
                            report({panoId: m[1], source: 'img:panoid'});
                        }
                    }
                    return origImgSrc.set.call(this, val);
                },
                get: origImgSrc.get ? function() { return origImgSrc.get.call(this); } : undefined,
                configurable: true
            });
        }
    } catch(e) {}

    // ─── PERIODIC SCAN ───
    function periodic() {
        for (var i = 0; i < window.__gg_panoramas.length; i++) {
            try {
                var pos = window.__gg_panoramas[i].getPosition();
                if (pos) reportLoc(pos.lat(), pos.lng(), 'periodic');
            } catch(e) {}
        }
        setTimeout(periodic, 700);
    }

    // ─── REACT SCAN ───
    function reactScan() {
        try {
            var root = document.querySelector('#root, #__next, #app');
            if (!root) return;
            var keys = Object.keys(root);
            for (var k of keys) {
                if (k.startsWith('__reactContainer$') || k.startsWith('__reactFiber$') || k.startsWith('_reactRootContainer')) {
                    var fiber = root[k];
                    if (fiber) scanFiber(fiber, 0);
                    break;
                }
            }
        } catch(e) {}
        setTimeout(reactScan, 1500);
    }

    function scanFiber(node, count) {
        if (!node || count > 300 || state.location) return;
        try {
            if (node.memoizedState) scanObj(node.memoizedState, 0);
            if (node.memoizedProps) scanObj(node.memoizedProps, 0);
        } catch(e) {}
        if (node.child) scanFiber(node.child, count+1);
        if (node.sibling) scanFiber(node.sibling, count+1);
    }

    function scanObj(obj, depth) {
        if (depth > 4 || !obj || typeof obj !== 'object' || state.location) return;
        if (typeof obj.lat === 'number' && typeof obj.lng === 'number') {
            if (Math.abs(obj.lat) <= 90 && Math.abs(obj.lng) <= 180 && (obj.lat !== 0 || obj.lng !== 0)) {
                reportLoc(obj.lat, obj.lng, 'react');
                return;
            }
        }
        if (obj.location && typeof obj.location === 'object') scanObj(obj.location, depth+1);
        for (var k of ['rounds','round','game','state','target','answer','currentRound','gameData']) {
            if (obj[k]) scanObj(obj[k], depth+1);
        }
    }

    // ─── INIT ───
    hookGM();
    periodic();
    reactScan();
    console.log('[GG] v3 injected');
})();
"""

# ═════════════════════════════════════════════════════════════════
# 5K MARKER — ставится на игровую карту GeoGuessr
# ═════════════════════════════════════════════════════════════════
FIVEK_SCRIPT = r"""
(function () {
    'use strict';

    // ══════════════════════════════════════════════════════════════
    //  СТИЛЬ — единственное место, которое нужно менять.
    //  Изменил → перезапустил main.py → нажал 5K → стиль обновится
    //  прямо в игре. Перезапускать игру НЕ нужно.
    // ══════════════════════════════════════════════════════════════
    var STYLE = {
        marker: {
            size:     46,
            color:    '#7c5cff',
            accent:   '#ffffff',
            fill:     'rgba(124,92,255,0.20)',
            strokeW:  2.5,
            outerR:   15,
            innerR:   7,
            dotR:     2.5,
            pulse:    true,          // пульсация внешнего кольца (SMIL-анимация внутри SVG)
            title:    '5K',
            zIndex:   999999
        },
        button: {
            label:    '5K',
            size:     56,
            left:     16,
            bottom:   16,
            radius:   '18px',
            bg:       'linear-gradient(160deg,#7c5cff 0%,#5b3df0 100%)',
            bgActive: 'linear-gradient(160deg,#6a4be8 0%,#4a30d6 100%)',
            bgOk:     'linear-gradient(160deg,#34d399 0%,#059669 100%)',
            bgErr:    'linear-gradient(160deg,#f87171 0%,#dc2626 100%)',
            fg:       '#ffffff',
            border:   '1px solid rgba(255,255,255,.18)',
            font:     '700 14px system-ui, -apple-system, sans-serif',
            shadow:   '0 6px 20px rgba(92,61,240,.35), inset 0 1px 0 rgba(255,255,255,.15)',
            shadowHi: '0 8px 28px rgba(92,61,240,.55), inset 0 1px 0 rgba(255,255,255,.2)',
            opacity:  0.82
        }
    };
    window.__GG5K_STYLE = STYLE;

    var M = window.__gg5K = window.__gg5K || {
        map: null, maps: [], marker: null, lastLoc: null, last: null, installed: false
    };
    var CLEAR_DIST = 500;

    // ══════════════════════════════════════════════════════════════
    //  CSS-АНИМАЦИИ ДЛЯ КНОПКИ (внедряются один раз в <head>)
    // ══════════════════════════════════════════════════════════════
    function injectCSS() {
        var old = document.getElementById('__gg5k_css');
        if (old) old.remove(); // позволяет обновлять анимации на лету при пере-инжекте
        var css = document.createElement('style');
        css.id = '__gg5k_css';
        css.textContent =
            '@keyframes gg5k-pop { 0%{transform:scale(.6);opacity:0} 60%{transform:scale(1.15);opacity:1} 100%{transform:scale(1);opacity:1} }' +
            '@keyframes gg5k-ripple { 0%{transform:scale(0);opacity:.55} 100%{transform:scale(2.6);opacity:0} }' +
            '@keyframes gg5k-spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }' +
            '@keyframes gg5k-idle-breathe { 0%,100%{filter:brightness(1)} 50%{filter:brightness(1.08)} }' +
            '#__gg_5k_btn{animation:gg5k-idle-breathe 3.6s ease-in-out infinite;}' +
            '#__gg_5k_btn .gg5k-icon-wrap{animation:gg5k-pop .35s cubic-bezier(.34,1.56,.64,1);}' +
            '#__gg_5k_btn .gg5k-ripple{position:absolute;inset:0;border-radius:inherit;background:rgba(255,255,255,.5);' +
                'animation:gg5k-ripple .5s ease-out forwards;pointer-events:none;}' +
            '#__gg_5k_btn .gg5k-spinner{animation:gg5k-spin .8s linear infinite;transform-origin:center;}';
        document.head.appendChild(css);
    }

    // ══════════════════════════════════════════════════════════════
    //  САМОПИСНЫЕ SVG-ИКОНКИ (вместо emoji-символов)
    // ══════════════════════════════════════════════════════════════
    function iconTarget(color) {
        return '<svg width="18" height="18" viewBox="0 0 18 18" fill="none">' +
            '<circle cx="9" cy="9" r="6.5" stroke="' + color + '" stroke-width="1.6" opacity=".9"/>' +
            '<circle cx="9" cy="9" r="2" fill="' + color + '"/>' +
            '</svg>';
    }
    function iconCheck(color) {
        return '<svg width="22" height="22" viewBox="0 0 22 22" fill="none">' +
            '<circle cx="11" cy="11" r="10" fill="none" stroke="' + color + '" stroke-width="1.4" opacity=".35"/>' +
            '<path d="M6 11.5L9.5 15L16 7.5" stroke="' + color + '" stroke-width="2.4" ' +
                'stroke-linecap="round" stroke-linejoin="round" fill="none"/>' +
            '</svg>';
    }
    function iconCross(color) {
        return '<svg width="22" height="22" viewBox="0 0 22 22" fill="none">' +
            '<circle cx="11" cy="11" r="10" fill="none" stroke="' + color + '" stroke-width="1.4" opacity=".35"/>' +
            '<path d="M7.5 7.5L14.5 14.5M14.5 7.5L7.5 14.5" stroke="' + color + '" stroke-width="2.4" ' +
                'stroke-linecap="round"/>' +
            '</svg>';
    }
    function iconSpinner(color) {
        return '<svg class="gg5k-spinner" width="20" height="20" viewBox="0 0 20 20" fill="none">' +
            '<circle cx="10" cy="10" r="7.5" stroke="' + color + '" stroke-width="2" opacity=".25"/>' +
            '<path d="M17.5 10a7.5 7.5 0 0 0-7.5-7.5" stroke="' + color + '" stroke-width="2" stroke-linecap="round"/>' +
            '</svg>';
    }
    function iconRemove(color) {
        return '<svg width="22" height="22" viewBox="0 0 22 22" fill="none">' +
            '<circle cx="11" cy="11" r="10" fill="none" stroke="' + color + '" stroke-width="1.4" opacity=".35"/>' +
            '<circle cx="11" cy="11" r="4.5" stroke="' + color + '" stroke-width="1.8" fill="none"/>' +
            '<path d="M6 6L16 16" stroke="' + color + '" stroke-width="2.2" stroke-linecap="round"/>' +
            '</svg>';
    }

    // ═══ SVG маркера на карте — "радар" с пульсацией через SMIL-анимацию ═══
    function buildMarkerIconSvg() {
        var S = STYLE.marker;
        var c = S.size / 2;
        var dash = (2 * Math.PI * S.outerR / 10).toFixed(1);
        var pulseAnim = S.pulse
            ? '<animate attributeName="r" values="' + S.outerR + ';' + (S.outerR + 4) + ';' + S.outerR +
              '" dur="2.2s" repeatCount="indefinite"/>' +
              '<animate attributeName="opacity" values="0.85;0.35;0.85" dur="2.2s" repeatCount="indefinite"/>'
            : '';

        return '<svg xmlns="http://www.w3.org/2000/svg" width="' + S.size + '" height="' + S.size
            + '" viewBox="0 0 ' + S.size + ' ' + S.size + '">'
            + '<circle cx="' + c + '" cy="' + c + '" r="' + (S.outerR + 3) + '" fill="rgba(0,0,0,0.18)"/>'
            + '<circle cx="' + c + '" cy="' + c + '" r="' + S.outerR + '" fill="none" stroke="' + S.color
            + '" stroke-width="' + S.strokeW + '" stroke-dasharray="' + dash + ' ' + dash + '" opacity="0.85">'
            + pulseAnim
            + '</circle>'
            + '<circle cx="' + c + '" cy="' + c + '" r="' + S.innerR + '" fill="' + S.fill
            + '" stroke="' + S.color + '" stroke-width="' + S.strokeW + '"/>'
            + '<circle cx="' + c + '" cy="' + c + '" r="' + S.dotR + '" fill="' + S.color + '"/>'
            + '<circle cx="' + (c - S.innerR * 0.4) + '" cy="' + (c - S.innerR * 0.4) + '" r="' + (S.dotR * 0.8)
            + '" fill="' + S.accent + '" opacity="0.7"/>'
            + '</svg>';
    }

    // ═══ кнопка оформляется из STYLE ═══
    function styleButton(b) {
        var S = STYLE.button;
        b.innerHTML =
            '<span class="gg5k-icon-wrap" style="display:flex;flex-direction:column;align-items:center;' +
            'justify-content:center;gap:3px;pointer-events:none;">'
            + iconTarget(S.fg)
            + '<span style="font:' + S.font + ';line-height:1;letter-spacing:.5px;">' + S.label + '</span>'
            + '</span>';

        b.style.cssText =
            'position:fixed;left:' + S.left + 'px;bottom:' + S.bottom + 'px;z-index:2147483647;'
            + 'width:' + S.size + 'px;height:' + S.size + 'px;border-radius:' + S.radius + ';'
            + 'display:flex;align-items:center;justify-content:center;overflow:hidden;'
            + 'background:' + S.bg + ';color:' + S.fg + ';border:' + S.border + ';'
            + 'cursor:pointer;user-select:none;box-sizing:border-box;'
            + 'box-shadow:' + S.shadow + ';opacity:' + S.opacity + ';'
            + 'transition:transform .15s ease,box-shadow .15s ease,opacity .15s ease,background .25s ease;';
        b.title = 'GG Assistant: поставить 5K-маркер (ПКМ — убрать)';
    }

    function curStyle() { return window.__GG5K_STYLE || STYLE; }

    // ═══ публичные функции (переопределяются при каждом инжекте) ═══
    window.__ggClear5K = function () {
        if (M.marker) {
            try { M.marker.setMap(null); } catch (e) {}
            M.marker = null;
        }
    };

    window.__ggPlace5K = function (lat, lng) {
        if (typeof lat !== 'number' || typeof lng !== 'number')
            return { ok: false, reason: 'bad-args' };
        if (!window.google || !window.google.maps)
            return { ok: false, reason: 'no-maps-api' };
        if (!M.map)
            return { ok: false, reason: 'no-map' };

        window.__ggClear5K();

        var g = window.google.maps;
        var S = STYLE.marker;

        try {
            M.marker = new g.Marker({
                position: { lat: lat, lng: lng },
                map: M.map,
                title: S.title,
                zIndex: S.zIndex,
                clickable: false,   // НЕ ТРОГАТЬ: клики проходят сквозь маркер
                draggable: false,
                icon: {
                    url: 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(buildMarkerIconSvg()),
                    anchor: new g.Point(S.size / 2, S.size / 2), // всегда size/2!
                    scaledSize: new g.Size(S.size, S.size)
                }
            });
            M.last = { lat: lat, lng: lng };
        } catch (e) {
            return { ok: false, reason: 'marker-error: ' + (e && e.message) };
        }

        try {
            var b = M.map.getBounds();
            if (b && !b.contains({ lat: lat, lng: lng })) M.map.panTo({ lat: lat, lng: lng });
        } catch (e) {}

        return { ok: true };
    };

    // ═══ повторный инжект = обновление стиля НА ЛЕТУ ═══
    injectCSS();
    if (M.installed) {
        if (M.marker && M.last) window.__ggPlace5K(M.last.lat, M.last.lng);
        var btn = document.getElementById('__gg_5k_btn');
        if (btn) styleButton(btn);
        return 'style-updated';
    }
    M.installed = true;

    // ═══ ниже — хуки (выполняется один раз) ═══

    function captureMap(m) {
        if (!m) return;
        if (M.maps.indexOf(m) === -1) M.maps.push(m);
        if (M.map !== m) {
            if (M.marker && M.marker.getMap && M.marker.getMap() !== m) M.marker = null;
            M.map = m;
            console.log('[GG] guess map captured');
        }
    }

    function hookMapsAPI() {
        if (!window.google || !window.google.maps || !window.google.maps.Map) {
            setTimeout(hookMapsAPI, 100);
            return;
        }
        var g = window.google.maps;

        try {
            var OrigMap = g.Map;
            var WrappedMap = function (el, opts) {
                var inst = new OrigMap(el, opts);
                captureMap(inst);
                return inst;
            };
            WrappedMap.prototype = OrigMap.prototype;
            Object.getOwnPropertyNames(OrigMap).forEach(function (k) {
                if (k !== 'prototype' && !(k in WrappedMap)) {
                    try { WrappedMap[k] = OrigMap[k]; } catch (e) {}
                }
            });
            g.Map = WrappedMap;
        } catch (e) {}

        try {
            ['setCenter', 'setZoom', 'panTo', 'panBy', 'fitBounds', 'setOptions'].forEach(function (n) {
                var orig = g.Map.prototype[n];
                if (typeof orig !== 'function' || orig.__gg5k) return;
                var wrapped = function () { captureMap(this); return orig.apply(this, arguments); };
                wrapped.__gg5k = true;
                g.Map.prototype[n] = wrapped;
            });
        } catch (e) {}

        try {
            var OrigMarker = g.Marker;
            if (OrigMarker) {
                var WrappedMarker = function (opts) {
                    var inst = new OrigMarker(opts);
                    try { captureMap((opts && opts.map) || inst.getMap()); } catch (e) {}
                    return inst;
                };
                WrappedMarker.prototype = OrigMarker.prototype;
                g.Marker = WrappedMarker;
            }
            var origSetMap = g.Marker.prototype.setMap;
            if (origSetMap && !origSetMap.__gg5k) {
                var wSetMap = function (m) { if (m) captureMap(m); return origSetMap.apply(this, arguments); };
                wSetMap.__gg5k = true;
                g.Marker.prototype.setMap = wSetMap;
            }
        } catch (e) {}

        try {
            var AM = g.marker && g.marker.AdvancedMarkerElement;
            if (AM) {
                var WrappedAM = function (opts) {
                    var inst = new AM(opts);
                    try { captureMap(opts && opts.map); } catch (e) {}
                    return inst;
                };
                WrappedAM.prototype = AM.prototype;
                g.marker.AdvancedMarkerElement = WrappedAM;
            }
        } catch (e) {}

        addButton();
    }

    function distMeters(a, b) {
        var R = 6371000;
        var dLat = (b.lat - a.lat) * Math.PI / 180;
        var dLng = (b.lng - a.lng) * Math.PI / 180;
        var la1 = a.lat * Math.PI / 180, la2 = b.lat * Math.PI / 180;
        var h = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                Math.cos(la1) * Math.cos(la2) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
        return 2 * R * Math.asin(Math.sqrt(h));
    }

    function follow() {
        try {
            var s = window.__gg_assistant;
            if (s && s.location && typeof s.location.lat === 'number') {
                if (M.lastLoc && distMeters(M.lastLoc, s.location) > CLEAR_DIST) {
                    window.__ggClear5K();
                }
                M.lastLoc = { lat: s.location.lat, lng: s.location.lng };
            }
        } catch (e) {}
        setTimeout(follow, 400);
    }

    // ═══ ripple-эффект при клике (декоративный слой поверх кнопки) ═══
    function spawnRipple(btn) {
        var r = document.createElement('span');
        r.className = 'gg5k-ripple';
        btn.appendChild(r);
        setTimeout(function () { r.remove(); }, 520);
    }

    function addButton() {
        if (document.getElementById('__gg_5k_btn')) return;
        if (!document.body) { setTimeout(addButton, 300); return; }
        var b = document.createElement('div');
        b.id = '__gg_5k_btn';
        styleButton(b);

        b.addEventListener('mouseenter', function () {
            var S = curStyle().button;
            b.style.opacity = '1';
            b.style.transform = 'translateY(-2px)';
            b.style.boxShadow = S.shadowHi;
        });
        b.addEventListener('mouseleave', function () {
            var S = curStyle().button;
            b.style.opacity = S.opacity;
            b.style.transform = 'translateY(0)';
            b.style.boxShadow = S.shadow;
        });
        b.addEventListener('mousedown', function () {
            var S = curStyle().button;
            b.style.transform = 'translateY(0) scale(0.93)';
            b.style.background = S.bgActive;
        });
        b.addEventListener('mouseup', function () {
            var S = curStyle().button;
            b.style.transform = 'translateY(-2px) scale(1)';
            b.style.background = S.bg;
        });

        b.addEventListener('click', function (ev) {
            ev.preventDefault(); ev.stopPropagation();
            spawnRipple(b);
            var s = window.__gg_assistant;
            var loc = s && s.location;
            if (!loc) { flash(b, 'pending'); return; }
            flash(b, 'pending');
            var r = window.__ggPlace5K(loc.lat, loc.lng);
            setTimeout(function () { flash(b, r && r.ok ? 'ok' : 'err'); }, 180);
        });
        b.addEventListener('contextmenu', function (ev) {
            ev.preventDefault(); ev.stopPropagation();
            spawnRipple(b);
            window.__ggClear5K();
            flash(b, 'removed');
        });
        document.body.appendChild(b);
    }

    // ═══ статусные состояния кнопки со своими иконками и цветами ═══
    function flash(btn, state) {
        var S = curStyle().button;
        var icon = '', bg = S.bg, label = S.label;

        if (state === 'pending') { icon = iconSpinner(S.fg); bg = S.bg; label = '···'; }
        else if (state === 'ok')  { icon = iconCheck(S.fg);   bg = S.bgOk;  label = 'OK'; }
        else if (state === 'err') { icon = iconCross(S.fg);   bg = S.bgErr; label = '—'; }
        else if (state === 'removed') { icon = iconRemove(S.fg); bg = S.bgActive; label = ''; }

        btn.style.background = bg;
        btn.innerHTML =
            '<span class="gg5k-icon-wrap" style="display:flex;flex-direction:column;align-items:center;' +
            'justify-content:center;gap:3px;pointer-events:none;">'
            + icon
            + (label ? '<span style="font:' + S.font + ';line-height:1;letter-spacing:.5px;">' + label + '</span>' : '')
            + '</span>';

        if (state !== 'pending') {
            setTimeout(function () { styleButton(btn); }, 850);
        }
    }

    hookMapsAPI();
    follow();
    return 'installed';
})();
"""

INJECTION_SCRIPT += "\n;\n" + FIVEK_SCRIPT
# ═════════════════════════════════════════════════════════════════
# FLASK
# ═════════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
CORS(app)
cdp = None

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")

@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(str(STATIC_DIR), filename)

@app.route("/api/place-5k", methods=["POST", "GET"])
def api_place_5k():
    if not cdp or not cdp.connected:
        return jsonify({"success": False, "error": "not connected to game"})
    data = request.get_json(silent=True) or {}
    lat, lng = data.get("lat"), data.get("lng")
    if lat is None or lng is None:
        lat, lng = request.args.get("lat"), request.args.get("lng")
    if lat is None or lng is None:
        loc = (cdp.location_data or {}).get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lng")
    if lat is None or lng is None:
        return jsonify({"success": False, "error": "no location known yet"})
    try:
        placed, reasons = cdp.place_5k(float(lat), float(lng))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    if placed > 0:
        return jsonify({"success": True, "placed": placed})
    reason = reasons[0] if reasons else "no game page responded"
    msg = reason
    if reason == "no-map":
        msg = "карта игры ещё не захвачена — подвигай/зазумь карту угадывания в игре и нажми 5K ещё раз"
    return jsonify({"success": False, "error": msg, "reason": reason})

@app.route("/api/clear-5k", methods=["POST", "GET"])
def api_clear_5k():
    if cdp and cdp.connected:
        cdp.clear_5k()
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "not connected"})
@app.route("/api/status")
def api_status():
    return jsonify({
        "connected": cdp.connected if cdp else False,
        "sessions": len(cdp.sessions) if cdp else 0,
        "page_urls": [v["targetInfo"].get("url","") for v in (cdp.sessions.values() if cdp else [])][:10],
    })

@app.route("/api/location")
def api_location():
    if cdp:
        return jsonify(cdp.location_data)
    return jsonify({"location": None})

@app.route("/api/history")
def api_history():
    if cdp:
        return jsonify(cdp.round_history)
    return jsonify([])

@app.route("/api/debug")
def api_debug():
    if cdp:
        return jsonify({
            "connected": cdp.connected,
            "sessions": {
                sid: {
                    "url": info["targetInfo"].get("url", ""),
                    "type": info["targetInfo"].get("type", ""),
                    "title": info["targetInfo"].get("title", ""),
                    "injected": info.get("injected", False),
                }
                for sid, info in cdp.sessions.items()
            },
            "location": cdp.location_data,
            "urls_seen": list(cdp._seen_urls)[-50:],
            "log": cdp._log_lines[-30:],
        })
    return jsonify({})

@app.route("/api/targets")
def api_targets():
    if cdp:
        targets = cdp.get_targets()
        return jsonify(targets)
    return jsonify([])

@app.route("/api/reverse-geocode")
def api_reverse_geocode():
    try:
        lat = float(request.args.get("lat", 0))
        lng = float(request.args.get("lng", 0))
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "lat": lat, "lon": lng,
            "format": "jsonv2",
            "zoom": 16,               # ← уровень улицы/района (было 3)
            "addressdetails": 1,
            "accept-language": "en",
        }
        headers = {"User-Agent": "GG-Assistant/3.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            addr = data.get("address", {}) or {}

            def pick(*keys):
                for k in keys:
                    v = addr.get(k)
                    if v:
                        return v
                return ""

            return jsonify({
                "success": True,
                "country": addr.get("country", ""),
                "country_code": (addr.get("country_code") or "").lower(),
                "display_name": data.get("display_name", ""),
                "city":      pick("city", "town", "village", "hamlet", "municipality"),
                "state":     pick("state", "province", "region"),
                "county":    pick("county", "district", "state_district"),
                "suburb":    pick("suburb", "neighbourhood", "city_district", "quarter", "borough"),
                "road":      pick("road", "pedestrian", "residential"),
                "postcode":  addr.get("postcode", ""),
            })
    except Exception as e:
        log.error(f"Geocode: {e}")
    return jsonify({"success": False, "country": "", "country_code": ""})


# ═════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════
def main():
    global cdp
    parser = argparse.ArgumentParser(description="GeoGuessr Assistant v3")
    parser.add_argument("--debug-port", type=int, default=34788)
    parser.add_argument("--ui-port", type=int, default=5000)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    print(r"""
╔══════════════════════════════════════════════════════════════╗
║   GeoGuessr Assistant v3                                     ║
║   Browser-level CDP + flat sessions                           ║
╚══════════════════════════════════════════════════════════════╝
    """)

    STATIC_DIR.mkdir(exist_ok=True)
    cdp = CDPConnection(args.debug_port)

    print(f"[*] Connecting: localhost:{args.debug_port}")

    if cdp.connect():
        print("[✓] Connected to browser")
    else:
        print("[!] Failed. Check launch options:")
        print(f"    --remote-debugging-port={args.debug_port}")

        def retry():
            while not cdp.connected:
                time.sleep(3)
                try:
                    cdp.connect()
                except:
                    pass
        threading.Thread(target=retry, daemon=True).start()

    # Poll
    def poll():
        while True:
            if cdp.connected:
                try:
                    cdp.poll()
                except:
                    pass
            time.sleep(0.5)
    threading.Thread(target=poll, daemon=True).start()

    print(f"\n[*] UI:      http://localhost:{args.ui_port}")
    print(f"[*] Debug:   http://localhost:{args.ui_port}/api/debug")
    print(f"[*] Targets: http://localhost:{args.ui_port}/api/targets")
    print("[*] Ctrl+C to stop\n")

    app.run(host="0.0.0.0", port=args.ui_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Exit")
        if cdp:
            cdp.disconnect()
        sys.exit(0)