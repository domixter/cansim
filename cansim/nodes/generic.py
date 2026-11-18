"""Wezly pomocnicze: analizator/nadajnik (np. komputer z adapterem USB-CAN) i slup bez odpowiedzi."""
from ..bus import BusNode, ErrorCounters, Frame


class GenericNode(ErrorCounters, BusNode):
    """Wezel, ktory potwierdza (ACK) ramki, zapamietuje odebrane i wysyla to, co mu zlecisz."""
    def __init__(self, name="PC", bitrate=250000, **_phys):
        self.name = name
        self.bitrate = bitrate
        self.ec_init()
        self.queue = []
        self.received = []
        self.sent = 0

    def send(self, frame: Frame):
        self.queue.append(frame)

    def tx_candidate(self):
        return (self.queue[0], 0) if self.queue else None

    def tx_done(self, handle, acked, now):
        self.queue.pop(0)
        self.sent += 1
        self.ec_tx_ok(now)

    def tx_error(self, now, kind="ack"):
        self.ec_tx_error(now, kind)       # ramka zostaje w kolejce i bedzie ponowiona

    def rx_error(self, now, lec=6):
        self.ec_rx_error(now)

    def next_event(self, now):
        return self.ec_next()

    def advance(self, now):
        self.ec_advance(now)

    def rx(self, frame, now):
        self.ec_rx_ok(now)
        self.received.append((now, frame))
        del self.received[:-200]
        return True

    def inject(self, what, value=None):
        if what == "power":
            self.online = bool(value)
            if not self.online:
                self.queue.clear()
        elif what == "bitrate":
            self.bitrate = int(value)

    def state_dict(self):
        return {"type": "generic", "name": self.name, "online": self.online, "bitrate": self.bitrate,
                "queued": len(self.queue), "sent": self.sent, "received": len(self.received), **self.ec_state()}
