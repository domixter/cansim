import struct
from ..bus import BusNode, ErrorCounters, Frame, NS

CMD = {"HEARTBEAT": 0x01, "AXIS_STATE": 0x07, "ENCODER": 0x09, "CTRL_MODE": 0x0B, "INPUT_POS": 0x0C,
       "INPUT_VEL": 0x0D, "CLEAR": 0x18, "POS_GAIN": 0x1A, "VEL_GAINS": 0x1B}
AXIS_ERR_WATCHDOG = 0x800000
STATES = {1: "IDLE", 3: "FULL_CALIBRATION", 8: "CLOSED_LOOP"}


class ODriveNode(ErrorCounters, BusNode):
    def __init__(self, node_id, bitrate=250000, hb_ms=100, enc_ms=10, vel_tau_ms=50, watchdog_ms=0, name=None, **_phys):
        self.node_id = node_id
        self.name = name or f"ODrive {node_id}"
        self.bitrate = bitrate
        self.ec_init()
        self.hb_ns, self.enc_ns = hb_ms * 1_000_000, enc_ms * 1_000_000
        self.vel_tau = vel_tau_ms / 1000.0
        self.watchdog_ns = watchdog_ms * 1_000_000
        self.state, self.error = 1, 0
        self.control_mode, self.input_mode = 2, 1
        self.input_vel, self.vel, self.pos = 0.0, 0.0, 0.0
        self.pos_gain, self.vel_gain, self.vel_i = 20.0, 0.16, 0.32
        self.mute_hb = False
        self.next_hb, self.next_enc, self.last_t = 0, 0, 0
        self.last_cmd_t = 0
        self.queue = []
        self.rx_count = 0

    # --- interfejs magistrali ---
    def tx_candidate(self):
        return (self.queue[0], 0) if self.queue else None

    def tx_done(self, handle, acked, now):
        self.queue.pop(0)
        self.ec_tx_ok(now)

    def tx_error(self, now, kind="ack"):
        self.ec_tx_error(now, kind)       # ramka zostaje w kolejce i jest ponawiana

    def rx_error(self, now, lec=6):
        self.ec_rx_error(now)

    def _send(self, cmd, payload):
        if len(self.queue) < 8:
            self.queue.append(Frame((self.node_id << 5) | cmd, payload))

    def rx(self, f, now):
        self.ec_rx_ok(now)
        if f.ext or f.rtr or (f.id >> 5) != self.node_id:
            return False
        cmd, d = f.id & 0x1F, f.data.ljust(8, b"\0")
        self.rx_count += 1
        self.last_cmd_t = now
        if cmd == CMD["AXIS_STATE"]:
            req = struct.unpack_from("<I", d)[0]
            if req in (1, 8) and (req == 1 or self.error == 0):
                self.state = req
        elif cmd == CMD["CTRL_MODE"]:
            self.control_mode, self.input_mode = struct.unpack_from("<II", d)
        elif cmd == CMD["INPUT_VEL"]:
            self.input_vel = struct.unpack_from("<f", d)[0]
        elif cmd == CMD["CLEAR"]:
            self.error = 0
        elif cmd == CMD["POS_GAIN"]:
            self.pos_gain = struct.unpack_from("<f", d)[0]
        elif cmd == CMD["VEL_GAINS"]:
            self.vel_gain, self.vel_i = struct.unpack_from("<ff", d)
        return True

    # --- czas ---
    def next_event(self, now):
        t = [self.next_enc, self.ec_next()]
        if not self.mute_hb:
            t.append(self.next_hb)
        return min(t)

    def advance(self, now):
        self.ec_advance(now)
        dt = (now - self.last_t) / NS
        self.last_t = now
        if dt > 0:
            target = self.input_vel if self.state == 8 else 0.0
            tau = self.vel_tau if self.state == 8 else 0.5
            self.vel += (target - self.vel) * min(1.0, dt / tau)
            self.pos += self.vel * dt
        if self.watchdog_ns and self.state == 8 and now - self.last_cmd_t > self.watchdog_ns:
            self.error |= AXIS_ERR_WATCHDOG
            self.state = 1
        if now >= self.next_enc:
            self.next_enc = now + self.enc_ns
            self._send(CMD["ENCODER"], struct.pack("<ff", self.pos, self.vel))
        if now >= self.next_hb:
            self.next_hb = now + self.hb_ns
            if not self.mute_hb:
                self._send(CMD["HEARTBEAT"], struct.pack("<IBBBB", self.error, self.state, 0, 0, 0))

    # --- sterowanie z UI ---
    def inject(self, what, value=None):
        if what == "error":
            self.error |= int(value or 0x1)
            self.state = 1
        elif what == "mute_hb":
            self.mute_hb = bool(value)
        elif what == "power":
            self.online = bool(value)
            if not self.online:
                self.queue.clear()
                self.state, self.vel = 1, 0.0
        elif what == "bitrate":
            self.bitrate = int(value)
        elif what == "watchdog_ms":
            self.watchdog_ns = int(value) * 1_000_000

    def state_dict(self):
        return {"type": "odrive", "name": self.name, "node_id": self.node_id, "online": self.online,
                "state": self.state, "state_name": STATES.get(self.state, str(self.state)), "error": self.error,
                "input_vel": round(self.input_vel, 3), "vel": round(self.vel, 3), "pos": round(self.pos, 3),
                "mute_hb": self.mute_hb, "rx": self.rx_count, "watchdog_ms": self.watchdog_ns // 1_000_000,
                "bitrate": self.bitrate, **self.ec_state()}


CMD_NAMES = {0x01: "heartbeat", 0x02: "estop", 0x03: "get_error", 0x07: "set_axis_state", 0x09: "encoder_estimates",
             0x0B: "set_controller_mode", 0x0C: "set_input_pos", 0x0D: "set_input_vel", 0x0E: "set_input_torque",
             0x0F: "set_limits", 0x16: "reboot", 0x17: "get_bus_voltage_current", 0x18: "clear_errors",
             0x1A: "set_pos_gain", 0x1B: "set_vel_gains"}


def decode(f):
    """Opis ramki w protokole ODrive CANSimple (node<<5 | cmd), little-endian."""
    if f.ext:
        return ""
    node, cmd = f.id >> 5, f.id & 0x1F
    name = CMD_NAMES.get(cmd, f"cmd 0x{cmd:02X}")
    d = f.data.ljust(8, b"\0")
    try:
        if cmd == 0x01 and f.dlc >= 5:
            err, st = struct.unpack_from("<IB", d)
            return f"node {node} {name}: blad=0x{err:X} stan={STATES.get(st, st)}"
        if cmd == 0x07:
            st = struct.unpack_from("<I", d)[0]
            return f"node {node} {name}: {STATES.get(st, st)}"
        if cmd == 0x09:
            p, v = struct.unpack_from("<ff", d)
            return f"node {node} {name}: poz={p:.3f} obr, v={v:.3f} obr/s"
        if cmd == 0x0B:
            cm, im = struct.unpack_from("<II", d)
            return f"node {node} {name}: control_mode={cm} input_mode={im}"
        if cmd == 0x0C:
            p, vff, tff = struct.unpack_from("<fhh", d)
            return f"node {node} {name}: poz={p:.3f} vel_ff={vff / 1000:.3f} torque_ff={tff / 1000:.3f}"
        if cmd == 0x0D:
            v, t = struct.unpack_from("<ff", d)
            return f"node {node} {name}: v={v:.3f} obr/s torque_ff={t:.3f}"
        if cmd in (0x0E, 0x1A):
            return f"node {node} {name}: {struct.unpack_from('<f', d)[0]:.4f}"
        if cmd == 0x1B:
            a, b = struct.unpack_from("<ff", d)
            return f"node {node} {name}: vel_gain={a:.4f} vel_integrator_gain={b:.4f}"
    except struct.error:
        pass
    return f"node {node} {name}"
