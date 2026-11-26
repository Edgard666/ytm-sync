"""Integration Home Assistant par MQTT (decouverte automatique des entites)."""
import json
import secrets
import threading
import time

from .store import VERSION

try:
    import paho.mqtt.client as paho
except ImportError:  # pragma: no cover
    paho = None


class MqttBridge:
    def __init__(self, store, runner, monitor, status_fn):
        self.store, self.runner, self.monitor, self.status_fn = store, runner, monitor, status_fn
        self.client = None
        self.sig = None          # config utilisee par la connexion courante
        self.connected = False
        self.last_error = None
        self._last_pub = 0
        self._was_running = None

    # ----------------------------------------------------------- configuration
    def cfg(self):
        return self.store.cfg.get("mqtt", {})

    def base(self):
        return (self.cfg().get("base_topic") or "ytm_sync").strip("/")

    def _signature(self):
        c = self.cfg()
        return (c.get("enabled"), c.get("host"), int(c.get("port") or 1883), c.get("user"), c.get("password"),
                c.get("tls"), c.get("discovery_prefix"), self.base())

    # ----------------------------------------------------------- boucle
    def loop(self):
        while True:
            try:
                sig = self._signature()
                if sig != self.sig:
                    if self.sig and self.sig[0] and not sig[0] and self.connected:
                        self.remove_discovery()      # MQTT desactive : retire les entites de Home Assistant
                    self._disconnect()
                    self.sig = sig
                    if sig[0] and sig[1] and paho:
                        self._connect()
                running = self.runner.running
                if self.connected and (time.time() - self._last_pub > 60 or running != self._was_running):
                    self.publish_state()
                self._was_running = running
            except Exception as ex:
                self.last_error = str(ex)
            time.sleep(3)

    def _connect(self):
        c = self.cfg()
        cl = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"ytm-sync-{secrets.token_hex(3)}")
        if c.get("user"):
            cl.username_pw_set(c["user"], c.get("password") or None)
        if c.get("tls"):
            cl.tls_set()
        cl.will_set(f"{self.base()}/availability", "offline", retain=True)
        cl.on_connect = self._on_connect
        cl.on_disconnect = self._on_disconnect
        cl.on_message = self._on_message
        cl.reconnect_delay_set(5, 120)
        self.client = cl
        self.last_error = None
        cl.connect_async(c["host"], int(c.get("port") or 1883), keepalive=60)
        cl.loop_start()

    def _disconnect(self):
        if self.client:
            try:
                self.client.publish(f"{self.base()}/availability", "offline", retain=True)
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:
                pass
        self.client, self.connected = None, False

    def _on_connect(self, cl, userdata, flags, rc, props=None):
        failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
        if failed:
            self.last_error = f"connexion refusée ({rc})"
            return
        self.connected, self.last_error = True, None
        b = self.base()
        cl.subscribe(f"{b}/cmd/#")
        cl.publish(f"{b}/availability", "online", retain=True)
        self.publish_discovery()
        self.publish_state()

    def _on_disconnect(self, cl, userdata, flags, rc, props=None):
        self.connected = False
        if (rc.is_failure if hasattr(rc, "is_failure") else rc != 0):
            self.last_error = f"déconnecté ({rc})"

    def _on_message(self, cl, userdata, msg):
        cmd = msg.topic.rsplit("/", 1)[-1]
        payload = msg.payload.decode(errors="ignore").strip().lower()
        if cmd == "sync":
            self.runner.start(trigger="Home Assistant (MQTT)", dry=True if payload == "dry" else None)
        elif cmd == "check":
            self.monitor.trigger()

    # ----------------------------------------------------------- publications
    def publish_discovery(self):
        pre = (self.cfg().get("discovery_prefix") or "homeassistant").strip("/")
        b = self.base()
        dev = {"identifiers": ["ytm_sync"], "name": "YTM Sync", "manufacturer": "YTM Sync",
               "model": "YouTube Music → Navidrome", "sw_version": VERSION}
        common = {"availability_topic": f"{b}/availability", "device": dev}
        sensors = [
            ("state", "État", "{{ value_json.state }}", {"icon": "mdi:sync"}),
            ("present", "Titres présents", "{{ value_json.counts.present }}", {"icon": "mdi:music-note", "state_class": "measurement"}),
            ("missing", "Titres manquants", "{{ value_json.counts.missing }}", {"icon": "mdi:music-note-off", "state_class": "measurement"}),
            ("requested", "Titres demandés", "{{ value_json.counts.requested }}", {"icon": "mdi:download", "state_class": "measurement"}),
            ("dl_active", "Téléchargements en cours", "{{ value_json.counts.dl_active }}", {"icon": "mdi:progress-download"}),
            ("dl_failed", "Échecs de téléchargement", "{{ value_json.counts.dl_failed }}", {"icon": "mdi:alert-circle"}),
            ("to_link", "Titres à lier", "{{ value_json.counts.unmatched }}", {"icon": "mdi:link-variant"}),
            ("ignored", "Titres ignorés", "{{ value_json.counts.ignored }}", {"icon": "mdi:eye-off"}),
            ("orphans", "Titres à faire le ménage", "{{ value_json.counts.orphans }}", {"icon": "mdi:broom"}),
            ("low_quality", "Titres en qualité faible", "{{ value_json.counts.low_quality }}", {"icon": "mdi:quality-low"}),
            ("coverage", "Couverture", "{{ value_json.coverage }}", {"unit_of_measurement": "%", "icon": "mdi:chart-line", "state_class": "measurement"}),
            ("last_sync", "Dernière synchro", "{{ value_json.last_run_iso }}", {"device_class": "timestamp"}),
            ("next_sync", "Prochaine synchro", "{{ value_json.next_run_iso }}", {"device_class": "timestamp"}),
        ]
        for oid, name, tpl, extra in sensors:
            self._pub(f"{pre}/sensor/ytm_sync/{oid}/config", dict(common, name=name, unique_id=f"ytm_sync_{oid}",
                      state_topic=f"{b}/state", value_template=tpl, **extra))
        self._pub(f"{pre}/binary_sensor/ytm_sync/running/config", dict(common, name="Synchro en cours",
                  unique_id="ytm_sync_running", state_topic=f"{b}/state", device_class="running",
                  value_template="{{ 'ON' if value_json.running else 'OFF' }}"))
        for oid, name, payload, icon in (("sync", "Lancer la synchro", "run", "mdi:play"),
                                         ("dry", "Lancer une simulation", "dry", "mdi:test-tube"),
                                         ("check", "Vérifier les téléchargements", "check", "mdi:refresh")):
            topic = f"{b}/cmd/check" if oid == "check" else f"{b}/cmd/sync"
            self._pub(f"{pre}/button/ytm_sync/{oid}/config", dict(common, name=name, unique_id=f"ytm_sync_btn_{oid}",
                      command_topic=topic, payload_press=payload, icon=icon))

    def _entities(self):
        pre = (self.cfg().get("discovery_prefix") or "homeassistant").strip("/")
        sensors = ["state", "present", "missing", "requested", "dl_active", "dl_failed", "to_link", "ignored",
                   "orphans", "coverage", "last_sync", "next_sync", "low_quality"]
        return ([f"{pre}/sensor/ytm_sync/{o}/config" for o in sensors] +
                [f"{pre}/binary_sensor/ytm_sync/running/config"] +
                [f"{pre}/button/ytm_sync/{o}/config" for o in ("sync", "dry", "check")])

    def remove_discovery(self):
        """Message vide retenu = Home Assistant supprime l'entite."""
        for t in self._entities():
            self.client.publish(t, "", retain=True)
        self.client.publish(f"{self.base()}/state", "", retain=True)

    def _pub(self, topic, obj):
        self.client.publish(topic, json.dumps(obj, ensure_ascii=False), retain=True)

    def publish_state(self):
        if not (self.client and self.connected):
            return
        self.client.publish(f"{self.base()}/state", json.dumps(self.status_fn(), ensure_ascii=False), retain=True)
        self._last_pub = time.time()

    # ----------------------------------------------------------- test depuis l'interface
    @staticmethod
    def test(c, timeout=8):
        if not paho:
            return False, "bibliothèque paho-mqtt absente"
        res, ev = {}, threading.Event()
        cl = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"ytm-sync-test-{secrets.token_hex(3)}")
        if c.get("user"):
            cl.username_pw_set(c["user"], c.get("password") or None)
        if c.get("tls"):
            cl.tls_set()

        def on_connect(cl_, u, f, rc, p=None):
            res["rc"] = rc
            ev.set()
        cl.on_connect = on_connect
        try:
            cl.connect(c["host"], int(c.get("port") or 1883), keepalive=10)
            cl.loop_start()
            if not ev.wait(timeout):
                return False, "pas de réponse du broker"
            rc = res["rc"]
            failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
            return (False, f"connexion refusée : {rc}") if failed else (True, "Connexion au broker OK")
        except Exception as ex:
            return False, str(ex)
        finally:
            try:
                cl.disconnect()
                cl.loop_stop()
            except Exception:
                pass
