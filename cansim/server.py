import argparse, json, mimetypes, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .app import App

WEB = Path(__file__).parent / "web"


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _trusted(self, need_token):
            """Ochrona przed innymi stronami w przegladarce: tylko Host lokalny (DNS rebinding), zgodny Origin
            i wlasny naglowek X-Cansim przy zmianach (wymusza preflight CORS, ktorego serwer nie obsluguje)."""
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            if host not in ("127.0.0.1", "localhost"):
                return False
            origin = self.headers.get("Origin")
            if origin and origin.rstrip("/") not in (f"http://127.0.0.1:{self.server.server_port}",
                                                     f"http://localhost:{self.server.server_port}"):
                return False
            return not need_token or self.headers.get("X-Cansim") == "1"

        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if not self._trusted(False):
                return self._send(403, '{"error": "dostep tylko z lokalnego interfejsu cansim"}')
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/state":
                return self._send(200, json.dumps(app.state(q.get("board"), q.get("periph"), q.get("allregs") == "1"), default=str))
            if u.path == "/api/files":
                return self._send(200, json.dumps(app.files(q.get("board"))))
            if u.path == "/api/file":
                t = app.read_file(q.get("board"), q["path"])
                return self._send(200 if t is not None else 404, json.dumps({"text": t}))
            if u.path == "/api/fs":
                return self._send(200, json.dumps(app.list_dir(q.get("path"))))
            if u.path == "/api/symbols":
                return self._send(200, json.dumps(app.symbols(q.get("board")), default=str))
            if u.path == "/api/complete":
                with app.lock:
                    return self._send(200, json.dumps(app.console.complete(q.get("line", "")), default=str))
            if u.path == "/api/examples":
                return self._send(200, json.dumps(app.examples()))
            if u.path == "/api/export":
                fmt = q.get("fmt", "candump")
                body = app.export(fmt)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="cansim.{"csv" if fmt == "csv" else "log"}"')
                self.end_headers()
                self.wfile.write(body.encode())
                return
            f = WEB / ("index.html" if u.path in ("/", "") else u.path.lstrip("/"))
            if f.is_file() and WEB in f.resolve().parents:
                return self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "text/plain")
            self._send(404, "{}")

        def do_POST(self):
            if not self._trusted(True):
                return self._send(403, '{"error": "dostep tylko z lokalnego interfejsu cansim"}')
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            p = urlparse(self.path).path
            ok = lambda msg="ok": self._send(200, json.dumps({"msg": msg}))
            try:
                if p == "/api/run":
                    app.running = bool(d.get("run"))
                    app.speed = float(d.get("speed", app.speed))
                    return ok()
                if p == "/api/step":
                    app.step(float(d.get("ms", 1)))
                    return ok()
                if p == "/api/reset":
                    app.reset()
                    return ok()
                if p == "/api/build":
                    app.rebuild(d.get("board"))
                    return ok()
                if p == "/api/call":
                    r, err = app.call(d.get("board"), d["fn"], d.get("args", []))
                    return self._send(200, json.dumps({"result": r, "error": err}))
                if p == "/api/inject":
                    return ok(app.inject(d["target"], d["what"], d.get("value")))
                if p == "/api/send":
                    return ok(app.send_frame(d["node"], d["id"], d.get("data", ""), d.get("ext", False)))
                if p == "/api/save":
                    return self._send(200, json.dumps({"ok": app.write_file(d.get("board"), d["path"], d["text"])}))
                if p == "/api/system/patch":
                    return ok(app.patch(d["id"], d.get("changes", {})))
                if p == "/api/system/bus":
                    return ok(app.set_bus(d))
                if p == "/api/system/add":
                    return ok(app.add(d["kind"], d.get("project"), name=d.get("name"), family=d.get("family")))
                if p == "/api/system/open":
                    return ok(app.open_system(d.get("path")))
                if p == "/api/board/config":
                    return ok(app.set_board_config(d["id"], d.get("changes", {})))
                if p == "/api/cmd":
                    okk, out = app.console.run(d.get("line", ""))
                    return self._send(200, json.dumps({"ok": okk, "out": out}))
                if p == "/api/script":
                    return self._send(200, json.dumps(app.console.script(d.get("text", "")), default=str))
                if p == "/api/scenario/save":
                    app.sys.setdefault("scenarios", {})[str(d["name"])] = d.get("text", "")
                    return ok()
                if p == "/api/watch/remove":
                    app.remove_watch(d["board"], d["name"])
                    return ok()
                if p == "/api/headers/fetch":
                    return ok(app.fetch_headers())
                if p == "/api/system/remove":
                    return ok(app.remove(d["id"]))
                if p == "/api/system/save":
                    return ok(app.save_system(d.get("path")))
            except Exception as ex:                   # noqa
                return self._send(500, json.dumps({"error": repr(ex)}))
            self._send(404, "{}")
    return H


def _banner(url, project):
    import os, shutil, sys
    from . import __version__
    from .cmsis import present
    if os.name == "nt":
        os.system("")                                   # wlacza kolory ANSI w konsoli Windows
        os.system("title cansim")
    G, Y, R, B, D, X = "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[2m", "\033[0m"
    ok = lambda c, t, bad: f"  {G}✓{X} {t}" if c else f"  {R}✗{X} {t}  {Y}{bad}{X}"
    gcc = shutil.which("gcc")
    lines = [
        "", f"  {B}cansim {__version__}{X}  symulator magistrali CAN dla STM32 (H7 FDCAN, F4 bxCAN)", "",
        ok(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "potrzebny Python 3.9 albo nowszy"),
        ok(bool(gcc), f"kompilator gcc: {gcc or 'brak'}", "zainstaluj gcc (README) i dodaj do PATH"),
        ok(present("h7"), "nagłówki ST dla STM32H7", "uruchom pobierz_naglowki_st.bat albo przycisk w programie"),
        ok(present("f4"), "nagłówki ST dla STM32F4", "uruchom pobierz_naglowki_st.bat albo przycisk w programie"),
        "", f"  Otwórz w przeglądarce:  {B}{url}{X}" + (f"   {D}({project}){X}" if project else ""),
        f"  {D}Zamknięcie: Ctrl+C w tym oknie. Okno musi zostać otwarte, dopóki używasz programu.{X}", "",
    ]
    try:
        print("\n".join(lines), flush=True)
    except UnicodeEncodeError:
        print(f"cansim: {url}  (Ctrl+C konczy)", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cansim", description="Symulator CAN dla firmware STM32 (H7 FDCAN, F4 bxCAN)")
    ap.add_argument("project", nargs="?", default=None,
                    help="opcjonalnie: cansim_system.json albo cansim_project.json; bez argumentu startuje pusty schemat")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    srv = None
    for port in range(a.port, a.port + 20):            # zajety port (np. program juz otwarty): bierzemy nastepny
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), None)
            break
        except OSError:
            continue
    if srv is None:
        print(f"cansim: porty {a.port}-{a.port + 19} sa zajete; zamknij inne okno cansim albo podaj --port")
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"cansim: uruchamianie (kompilacja plytek moze chwile potrwac)...", flush=True)
    app = App(a.project)
    srv.RequestHandlerClass = make_handler(app)
    _banner(url, a.project)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("cansim: zamknieto")
