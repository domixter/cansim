import threading
from collections import deque

from .bus import Bus


class World:
    def __init__(self):
        self.now = 0
        self.bus = Bus()
        self.boards = []          # Engine
        self.nodes = []           # ODrive, generyczne itp.
        self.events = deque(maxlen=3000)
        self.lock = threading.RLock()
        self.depth = 0

    def add_board(self, eng):
        self.boards.append(eng)
        if eng.can:
            self.bus.add(eng.can)

    def remove_board(self, eng):
        if eng in self.boards:
            self.boards.remove(eng)
        if eng.can:
            self.bus.remove(eng.can)
        for t in eng.tasks:
            t.done = True

    def add_node(self, node):
        self.nodes.append(node)
        self.bus.add(node)
        return node

    def remove_node(self, node):
        if node in self.nodes:
            self.nodes.remove(node)
        self.bus.remove(node)

    def event(self, kind, text):
        self.events.append((self.now, kind, text, ""))

    def _next_time(self, t_end):
        c = [t_end]
        for b in self.boards:
            b.next_times(c)
        for n in self.nodes:
            c.append(n.next_event(self.now))
        nb = self.bus.next_event(self.now)
        if nb is not None:
            c.append(nb)
        return max(min(c), self.now)

    def _process(self):
        for _ in range(50):
            progressed = False
            nb = self.bus.next_event(self.now)
            if nb is not None and nb <= self.now:
                self.bus.step(self.now)
                progressed = True
            for b in self.boards:
                progressed |= b.advance_models()
            for n in self.nodes:
                if n.next_event(self.now) <= self.now:
                    n.advance(self.now)
                    progressed = True
            if not progressed:
                break
        for b in list(self.boards):
            b.interrupts()

    def run_until(self, t_end):
        self.depth += 1
        try:
            guard = 0
            while True:
                for b in list(self.boards):
                    b.resume_due()
                nt = self._next_time(t_end)
                if nt > self.now:
                    self.now = nt
                    guard = 0
                else:
                    guard += 1
                    if guard > 5000:
                        self.event("uwaga", "silnik nie posuwa czasu (pętla zdarzeń w jednej chwili)")
                        self.now += 1000
                        guard = 0
                self._process()
                if self.now >= t_end and not any(b.has_due_task() for b in self.boards):
                    break
        finally:
            self.depth -= 1

    def step_ms(self, ms):
        with self.lock:
            self.run_until(self.now + int(ms * 1_000_000))
