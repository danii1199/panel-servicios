#!/usr/bin/env python3
"""Panel de servicios: ver qué está arrancado y arrancarlo o pararlo.

OpenClaw, Creador, Ollama, Docker, los servicios que registres tú (Python, Node…) y cualquier otro programa tuyo
que esté escuchando en un puerto (detectado solo).

Aplicación de escritorio GTK3. Las comprobaciones y acciones se hacen en hilos aparte y la ventana se actualiza
con GLib.idle_add, así la interfaz nunca se queda bloqueada.
"""
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import configuracion as cfg  # noqa: E402

HOME = Path.home()
# se pueden cambiar desde el panel (⚙): se guardan en ~/.config/panel-servicios/ajustes.json
_AJ = cfg.leer_ajustes()
CREADOR_DIR = Path(os.path.expanduser(_AJ["creador_dir"]))
CREADOR_PORT = int(_AJ["creador_puerto"])
GATEWAY_PORT = 18789
OLLAMA = _AJ["ollama_url"].rstrip("/")
REFRESCO_S = int(_AJ["refresco_s"])
CONFIG = HOME / ".config/panel-servicios/servicios.json"   # servicios registrados
LOGS = HOME / ".local/state/panel-servicios/logs"           # salida de los que arranca el panel

# Programas que cuentan como "servidor de desarrollo" en la detección automática (nombre del proceso).
DETECTAR = re.compile(r"^(python[\d.]*|node|nodejs|deno|bun|npm|npx|pnpm|yarn|vite|uvicorn|gunicorn|flask|"
                      r"hypercorn|daphne|ruby|rails|puma|php|php-fpm|java|dotnet|perl|go|air|cargo|hugo|jekyll)$")

CSS = b"""
.seccion { font-weight: bold; font-size: 1.05em; margin-top: 14px; }
.detalle { color: alpha(currentColor, 0.65); font-size: 0.9em; }
.on  { color: #1f8a4c; font-size: 1.3em; }
.off { color: #c53030; font-size: 1.3em; }
.na  { color: #9a948a; font-size: 1.3em; }
.fila { padding: 8px 6px 8px 10px; }
.vacio { padding: 10px 12px; color: alpha(currentColor, 0.6); font-size: 0.9em; }
"""


def run(cmd, timeout=20):
    """Ejecuta un comando y devuelve (ok, salida)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:  # comando inexistente, tiempo agotado…
        return False, str(e)


def http_json(url, data=None, timeout=3):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


ESTADOS = {"active": "en marcha", "inactive": "parado", "failed": "con error", "activating": "arrancando",
           "deactivating": "parando"}


def puertos_escuchando():
    """{pid: [puertos]} de los procesos del usuario que escuchan en TCP (ss solo enseña el pid de los nuestros)."""
    _, out = run(["ss", "-ltnpH"])
    res = {}
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 6:
            continue
        port = cols[3].rsplit(":", 1)[-1]
        for pid in re.findall(r"pid=(\d+)", cols[5]):
            lst = res.setdefault(int(pid), [])
            if port.isdigit() and int(port) not in lst:
                lst.append(int(port))
    return res


def pid_en_puerto(port, mapa=None):
    for pid, ports in (mapa if mapa is not None else puertos_escuchando()).items():
        if port in ports:
            return pid
    return None


def info_proceso(pid):
    """(nombre, argumentos, carpeta) de un proceso, o None si ya no existe o no es nuestro."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        args = [a.decode(errors="replace") for a in args if a]
        cwd = os.readlink(f"/proc/{pid}/cwd")
        return comm, args, cwd
    except OSError:
        return None


def corto(ruta):
    return str(ruta).replace(str(HOME), "~", 1)


HIJOS = []   # procesos lanzados por el panel: hay que recogerlos al terminar para que no queden "zombis"


def recoger_hijos():
    for p in HIJOS[:]:
        if p.poll() is not None:
            HIJOS.remove(p)


def vivo(pid):
    recoger_hijos()
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def parar_proceso(pid):
    """Para el proceso y los que lanzó con él (su grupo), salvo que el grupo sea el del propio panel."""
    try:
        pgid = os.getpgid(pid)
        if pgid != os.getpgid(0) and pgid > 1:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(20):  # espera hasta 5 s a que termine
        if not vivo(pid):
            return True, ""
        time.sleep(0.25)
    return False, f"El proceso {pid} no ha terminado. Si está colgado: kill -9 {pid}"


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "servicio"


# ---------- servicios ----------
# Cada servicio sabe comprobar su estado y arrancarse/pararse. estado() devuelve
# {"on": True/False/None, "detalle": str, "inicio": True/False/None (arranque al encender)}.

class SystemdUser:
    def __init__(self, nombre, unidad, desc):
        self.nombre, self.unidad, self.desc = nombre, unidad, desc

    def estado(self):
        _, act = run(["systemctl", "--user", "is-active", self.unidad])
        _, ena = run(["systemctl", "--user", "is-enabled", self.unidad])
        return {"on": act == "active", "detalle": f"{self.desc} · {ESTADOS.get(act, act)}", "inicio": ena == "enabled"}

    def arrancar(self):
        return run(["systemctl", "--user", "start", self.unidad])

    def parar(self):
        return run(["systemctl", "--user", "stop", self.unidad], timeout=60)

    def set_inicio(self, si):
        return run(["systemctl", "--user", "enable" if si else "disable", self.unidad])

    def fuente_log(self):
        # lo que escribe un .timer está en el journal de su .service
        unidad = self.unidad.replace(".timer", ".service")
        return {"titulo": self.nombre, "cmd": ["journalctl", "--user", "-u", unidad, "--no-hostname"]}


class SystemdSistema:
    """Servicio del sistema (Docker): cambiarlo pide la contraseña con pkexec."""

    def __init__(self, nombre, unidades, desc):
        self.nombre, self.unidades, self.desc = nombre, unidades, desc

    def estado(self):
        _, act = run(["systemctl", "is-active", self.unidades[0]])
        _, ena = run(["systemctl", "is-enabled", self.unidades[0]])
        return {"on": act == "active", "detalle": f"{self.desc} · {ESTADOS.get(act, act)}", "inicio": ena == "enabled"}

    def arrancar(self):
        return run(["pkexec", "systemctl", "start", *self.unidades], timeout=120)

    def parar(self):
        # el socket también: si no, Docker vuelve a arrancar en cuanto alguien lo llama
        return run(["pkexec", "systemctl", "stop", *reversed(self.unidades)], timeout=120)

    def set_inicio(self, si):
        return run(["pkexec", "systemctl", "enable" if si else "disable", *self.unidades], timeout=120)

    def fuente_log(self):
        return {"titulo": self.nombre, "cmd": ["journalctl", "-u", self.unidades[-1], "--no-hostname"]}


class Creador:
    nombre = "Creador de agentes y visor"

    def estado(self, mapa=None):
        pid = pid_en_puerto(CREADOR_PORT, mapa)
        det = f"http://127.0.0.1:{CREADOR_PORT} · " + (f"en marcha (pid {pid})" if pid else "parado")
        return {"on": pid is not None, "detalle": det, "inicio": None}

    def arrancar(self):
        log = open(CREADOR_DIR / "creador.log", "a")
        HIJOS.append(subprocess.Popen(["python3", str(CREADOR_DIR / "app.py")], cwd=CREADOR_DIR, stdout=log,
                                      env={**os.environ, "CREADOR_PORT": str(CREADOR_PORT)},
                                      stderr=log, stdin=subprocess.DEVNULL, start_new_session=True))
        for _ in range(20):
            if pid_en_puerto(CREADOR_PORT):
                return True, ""
            time.sleep(0.25)
        return False, "No ha arrancado: mira creador.log"

    def parar(self):
        pid = pid_en_puerto(CREADOR_PORT)
        return parar_proceso(pid) if pid else (True, "")

    def abrir(self):
        return run(["xdg-open", f"http://127.0.0.1:{CREADOR_PORT}"])

    def fuente_log(self):
        return {"titulo": self.nombre, "archivo": CREADOR_DIR / "creador.log"}


class Registrado:
    """Servicio que el usuario ha añadido: nombre, carpeta, comando y (opcional) puerto."""

    def __init__(self, d):
        self.d = d
        self.nombre = d["nombre"]
        self.carpeta = os.path.expanduser(d["carpeta"])
        self.comando = d["comando"]
        self.puerto = int(d["puerto"]) if str(d.get("puerto") or "").isdigit() else None
        self.log = LOGS / (slug(self.nombre) + ".log")
        # lo que buscar en la línea de comandos para reconocerlo (el script, o el comando entero)
        try:
            toks = shlex.split(self.comando)
        except ValueError:
            toks = self.comando.split()
        self.huella = next((t for t in reversed(toks) if "." in t and not t.startswith("-") and "/" not in t[:1]),
                           None) or (toks[-1] if toks else self.comando)

    def buscar_pid(self, mapa):
        if self.puerto:
            return pid_en_puerto(self.puerto, mapa)
        real = os.path.realpath(self.carpeta)
        for p in sorted(int(x) for x in os.listdir("/proc") if x.isdigit()):
            if p == os.getpid() or not vivo(p):
                continue
            inf = info_proceso(p)
            if inf and os.path.realpath(inf[2]) == real and self.huella in " ".join(inf[1]):
                return p
        return None

    def estado(self, mapa):
        pid = self.buscar_pid(mapa)
        det = f"{corto(self.carpeta)} · {self.comando}"
        if self.puerto:
            det = f"puerto {self.puerto} · " + det
        det += f" · en marcha (pid {pid})" if pid else " · parado"
        return {"on": pid is not None, "detalle": det, "inicio": None}

    def arrancar(self):
        if not os.path.isdir(self.carpeta):
            return False, f"La carpeta no existe: {self.carpeta}"
        LOGS.mkdir(parents=True, exist_ok=True)
        log = open(self.log, "a")
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {self.comando}\n")
        log.flush()
        # bash -c: así el comando puede llevar "source .venv/bin/activate && …"
        p = subprocess.Popen(["bash", "-c", self.comando], cwd=self.carpeta, stdout=log, stderr=log,
                             env={**os.environ, **{k: str(v) for k, v in (self.d.get("variables") or {}).items()}},
                             stdin=subprocess.DEVNULL, start_new_session=True)
        HIJOS.append(p)
        for _ in range(24):  # hasta 6 s para que aparezca (o abra su puerto)
            time.sleep(0.25)
            if p.poll() is not None:
                cola = self.log.read_text(errors="replace")[-600:]
                return False, f"Ha terminado nada más arrancar (código {p.returncode}).\n\n{cola}"
            if self.buscar_pid(puertos_escuchando()):
                return True, ""
        return True, ""  # sigue vivo aunque aún no escuche: el siguiente refresco lo mostrará

    def parar(self):
        pid = self.buscar_pid(puertos_escuchando())
        return parar_proceso(pid) if pid else (True, "")

    def abrir(self):
        return run(["xdg-open", f"http://127.0.0.1:{self.puerto}"])

    def fuente_log(self):
        return {"titulo": self.nombre, "archivo": self.log}


class Detectado:
    """Programa de desarrollo escuchando en un puerto que no está registrado."""

    def __init__(self, pid, comm, args, cwd, puertos):
        self.pid, self.comm, self.args, self.cwd, self.puertos = pid, comm, args, cwd, sorted(puertos)
        cmd = " ".join(Path(a).name if i == 0 else a for i, a in enumerate(args))
        self.cmd = corto(cmd)
        self.nombre = self.cmd if len(self.cmd) <= 60 else self.cmd[:57] + "…"

    def estado(self):
        det = "puerto " + ", ".join(map(str, self.puertos)) + f" · {corto(self.cwd)} · pid {self.pid}"
        return {"on": True, "detalle": det, "inicio": None}

    def parar(self):
        return parar_proceso(self.pid)

    def abrir(self):
        return run(["xdg-open", f"http://127.0.0.1:{self.puertos[0]}"])

    def fuente_log(self):
        """Solo si su salida va a un archivo (si va a una terminal, el log está en esa terminal)."""
        for fd in (1, 2):
            try:
                destino = os.readlink(f"/proc/{self.pid}/fd/{fd}")
            except OSError:
                continue
            if os.path.isfile(destino):
                return {"titulo": self.nombre, "archivo": Path(destino)}
        return None


class Contenedor:
    def __init__(self, nombre, proyecto, compose="", carpeta="", servicio=""):
        self.nombre, self.proyecto = nombre, proyecto
        self.compose, self.carpeta, self.servicio = compose.split(",")[0], carpeta, servicio

    def arrancar(self):
        if self.compose and os.path.isfile(self.compose) and self.servicio:
            # con compose y no con docker start: así se aplica lo que se haya cambiado en su docker-compose.yml
            return run(["docker", "compose", "-f", self.compose, "--project-directory",
                        self.carpeta or os.path.dirname(self.compose), "up", "-d", "--no-deps", self.servicio],
                       timeout=300)
        return run(["docker", "start", self.nombre], timeout=120)

    def parar(self):
        return run(["docker", "stop", self.nombre], timeout=120)

    def fuente_log(self):
        return {"titulo": "Contenedor " + self.nombre, "cmd": ["docker", "logs", "--timestamps", self.nombre]}


def contenedores():
    """Lista de contenedores con su estado, o None si Docker no responde."""
    ok, out = run(["docker", "ps", "-a", "--format",
                   '{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}\t{{.Ports}}\t'
                   '{{.Label "com.docker.compose.project.config_files"}}\t'
                   '{{.Label "com.docker.compose.project.working_dir"}}\t{{.Label "com.docker.compose.service"}}'],
                  timeout=8)
    if not ok:
        return None
    res = []
    for line in out.splitlines():
        n, st, status, proj, ports, compose, carpeta, servicio = (line.split("\t") + [""] * 8)[:8]
        det = status + (f" · {ports}" if ports else "")
        res.append((Contenedor(n, proj, compose, carpeta, servicio), {"on": st == "running", "detalle": det, "inicio": None}))
    return res


def ollama_estado():
    try:
        ps = http_json(OLLAMA + "/api/ps", timeout=2).get("models", [])
    except Exception:
        return {"on": False, "detalle": f"{OLLAMA} · no responde (¿está encendido el otro ordenador?)",
                "inicio": None, "modelos": []}
    if ps:
        cargados = ", ".join(f"{m['name']} ({m.get('size_vram', 0) / 1e9:.1f} GB en GPU)" for m in ps)
        det = f"{OLLAMA} · cargado: {cargados}"
    else:
        det = f"{OLLAMA} · responde, ningún modelo cargado en memoria"
    return {"on": True, "detalle": det, "inicio": None, "modelos": [m["name"] for m in ps]}


def ollama_liberar(modelos):
    for m in modelos:
        try:
            http_json(OLLAMA + "/api/generate", {"model": m, "keep_alive": 0}, timeout=30)
        except Exception as e:
            return False, str(e)
    return True, ""


LINEAS_LOG = 500


def leer_log(fuente):
    """Últimas LINEAS_LOG líneas de un log: un archivo o la salida de journalctl / docker logs."""
    if "archivo" in fuente:
        ruta = fuente["archivo"]
        try:
            with open(ruta, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 256 * 1024))  # basta con el final, aunque el log sea enorme
                texto = f.read().decode(errors="replace")
        except FileNotFoundError:
            return f"(Aún no hay log: {corto(ruta)} no existe. Se crea al arrancarlo desde el panel.)"
        except OSError as e:
            return f"(No se puede leer {corto(ruta)}: {e})"
        return "\n".join(texto.splitlines()[-LINEAS_LOG:]) or "(El log está vacío)"
    cmd = list(fuente["cmd"])
    if cmd[0] == "journalctl":
        cmd += ["-n", str(LINEAS_LOG), "--no-pager"]
    elif cmd[:2] == ["docker", "logs"]:
        cmd[2:2] = ["--tail", str(LINEAS_LOG)]
    try:
        # stderr junto con stdout y en orden: docker logs manda a stderr lo que el contenedor escribe allí
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                           timeout=15)
        return r.stdout.rstrip() or "(No hay nada en el log)"
    except Exception as e:
        return f"(No se pudo ejecutar {' '.join(cmd)}: {e})"


def cargar_registrados():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return []


def guardar_registrados(lista):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(lista, ensure_ascii=False, indent=2) + "\n")


def comando_para(archivo):
    """Comando sugerido para lanzar un script, usando el entorno virtual de su carpeta si lo hay."""
    carpeta, nombre = os.path.dirname(archivo), os.path.basename(archivo)
    if nombre.endswith(".py"):
        for venv in (".venv", "venv", "env"):
            py = os.path.join(carpeta, venv, "bin", "python")
            if os.path.exists(py):
                return f"{venv}/bin/python {shlex.quote(nombre)}"
        return f"python3 {shlex.quote(nombre)}"
    if nombre.endswith((".js", ".mjs", ".cjs")):
        return f"node {shlex.quote(nombre)}"
    if nombre.endswith(".ts"):
        return f"npx tsx {shlex.quote(nombre)}"
    if nombre == "package.json":
        return "npm start"
    if nombre.endswith(".sh"):
        return f"bash {shlex.quote(nombre)}"
    return "./" + shlex.quote(nombre)


# ---------- interfaz ----------

class Fila(Gtk.ListBoxRow):
    """Izquierda: interruptor para arrancar/parar y el piloto de estado (verde en marcha, rojo parado).
    Derecha: engranaje con la configuración y acciones de la fila.
    opciones: [(icono, texto, fn, en_hilo)]. en_hilo=True: fn devuelve (ok, msg) y se ejecuta en segundo plano
    (Abrir, Liberar GPU…); False: se ejecuta en la ventana (diálogos)."""

    def __init__(self, app, nombre, servicio=None, opciones=(), controlable=True):
        super().__init__()
        self.app, self.servicio = app, servicio
        self.set_activatable(False)
        caja = Gtk.Box(spacing=12)
        caja.get_style_context().add_class("fila")

        # columna izquierda: interruptor (vacía si no se puede controlar), y el spinner en su sitio mientras trabaja
        self.izq = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.izq.set_size_request(52, -1)
        self.interruptor = None
        if controlable:
            self.interruptor = Gtk.Switch(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
            self.interruptor_id = self.interruptor.connect("state-set", self.on_interruptor)
            self.izq.add_named(self.interruptor, "control")
        else:
            self.izq.add_named(Gtk.Box(), "control")
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.izq.add_named(self.spinner, "ocupado")
        caja.pack_start(self.izq, False, False, 0)
        self.punto = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
        caja.pack_start(self.punto, False, False, 0)

        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.titulo = Gtk.Label(xalign=0)
        self.titulo.set_ellipsize(Pango.EllipsizeMode.END)
        self.titulo.set_markup(f"<b>{GLib.markup_escape_text(nombre)}</b>")
        self.detalle = Gtk.Label(xalign=0)
        self.detalle.set_ellipsize(Pango.EllipsizeMode.END)
        self.detalle.get_style_context().add_class("detalle")
        textos.pack_start(self.titulo, False, False, 0)
        textos.pack_start(self.detalle, False, False, 0)
        caja.pack_start(textos, True, True, 0)

        # derecha: engranaje → menú con la configuración
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=10)
        cab = Gtk.Label(xalign=0, margin_bottom=6)
        cab.set_markup(f"<b>{GLib.markup_escape_text(nombre)}</b>")
        cab.set_ellipsize(Pango.EllipsizeMode.END)
        cab.set_max_width_chars(40)
        menu.pack_start(cab, False, False, 0)
        self.inicio = None
        if servicio is not None and hasattr(servicio, "set_inicio"):
            fila_ini = Gtk.Box(spacing=12, margin_bottom=4)
            lab = Gtk.Label(label="Arrancar al encender el ordenador", xalign=0)
            fila_ini.pack_start(lab, True, True, 0)
            self.inicio = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.inicio_id = self.inicio.connect("state-set", self.on_inicio)
            fila_ini.pack_start(self.inicio, False, False, 0)
            menu.pack_start(fila_ini, False, False, 0)
        opciones = list(opciones)
        if servicio is not None and hasattr(servicio, "fuente_log") and servicio.fuente_log():
            # self.servicio y no servicio: al refrescar, la fila puede recibir un objeto nuevo
            pos = 0
            while pos < len(opciones) and opciones[pos][1].startswith(("Configuración", "Abrir")):
                pos += 1
            opciones.insert(pos,
                            ("text-x-generic-symbolic", "Ver logs",
                             lambda: self.app.ver_log(self.servicio.fuente_log()), False))
        if self.inicio is not None and opciones:
            menu.pack_start(Gtk.Separator(margin_top=4, margin_bottom=4), False, False, 0)
        self.popover = Gtk.Popover()
        for icono, texto, fn, en_hilo in opciones:
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
            linea = Gtk.Box(spacing=10)
            linea.pack_start(Gtk.Image.new_from_icon_name(icono, Gtk.IconSize.MENU), False, False, 0)
            linea.pack_start(Gtk.Label(label=texto, xalign=0), True, True, 0)
            b.add(linea)
            b.connect("clicked", self.on_opcion, fn, en_hilo)
            menu.pack_start(b, False, False, 0)
        menu.show_all()
        self.popover.add(menu)
        self.engranaje = Gtk.MenuButton(popover=self.popover, valign=Gtk.Align.CENTER,
                                        relief=Gtk.ReliefStyle.NONE, tooltip_text="Configuración")
        self.engranaje.add(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON))
        self.engranaje.set_sensitive(bool(opciones) or self.inicio is not None)
        caja.pack_end(self.engranaje, False, False, 0)

        self.add(caja)
        self.on = None

    def actualizar(self, est):
        self.on = est["on"]
        self.detalle.set_text(est["detalle"])
        self.detalle.set_tooltip_text(est["detalle"])
        if self.interruptor is not None:
            self.interruptor.handler_block(self.interruptor_id)
            self.interruptor.set_active(bool(est["on"]))
            self.interruptor.set_state(bool(est["on"]))
            self.interruptor.handler_unblock(self.interruptor_id)
            # un detectado no se puede volver a arrancar: solo se deja apagar
            self.interruptor.set_sensitive(bool(est["on"]) or hasattr(self.servicio, "arrancar"))
            self.interruptor.set_tooltip_text("En marcha: pulsa para pararlo" if est["on"]
                                              else "Parado: pulsa para arrancarlo")
        ctx = self.punto.get_style_context()
        for c in ("on", "off", "na"):
            ctx.remove_class(c)
        ctx.add_class("na" if est["on"] is None else "on" if est["on"] else "off")
        self.punto.set_tooltip_text("Sin datos" if est["on"] is None else "En marcha" if est["on"] else "Parado")
        if self.inicio is not None and est.get("inicio") is not None:
            self.inicio.handler_block(self.inicio_id)
            self.inicio.set_active(est["inicio"])
            self.inicio.set_state(est["inicio"])
            self.inicio.handler_unblock(self.inicio_id)

    def ocupado(self, si):
        (self.spinner.start if si else self.spinner.stop)()
        self.izq.set_visible_child_name("ocupado" if si else "control")
        if self.inicio is not None:
            self.inicio.set_sensitive(not si)

    def on_interruptor(self, _sw, quiere):
        self.app.accion(self, self.servicio.arrancar if quiere else self.servicio.parar)
        return True  # el interruptor no cambia hasta que la comprobación confirme el estado real

    def on_inicio(self, _sw, si):
        self.app.accion(self, lambda: self.servicio.set_inicio(si))
        return True

    def on_opcion(self, _b, fn, en_hilo):
        self.popover.popdown()
        if en_hilo:
            self.app.accion(self, fn)
        else:
            fn()


class DialogoServicio(Gtk.Dialog):
    """Formulario para añadir o editar un servicio registrado."""

    def __init__(self, parent, datos=None):
        editar = datos is not None and "_editar" in datos
        super().__init__(title="Editar servicio" if editar else "Añadir servicio", transient_for=parent, modal=True)
        self.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok = self.add_button("Guardar", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        datos = datos or {}

        g = Gtk.Grid(column_spacing=12, row_spacing=10, margin=16)
        self.get_content_area().add(g)

        elegir = Gtk.Button(label="📂 Elegir script…")
        elegir.set_tooltip_text("Elige un .py, .js, .sh o package.json: se rellenan solos carpeta, comando y nombre")
        elegir.connect("clicked", self.on_elegir)
        g.attach(Gtk.Label(label="Rápido:", xalign=1), 0, 0, 1, 1)
        g.attach(elegir, 1, 0, 1, 1)

        self.nombre = Gtk.Entry(text=datos.get("nombre", ""), activates_default=True, width_chars=40)
        self.nombre.set_placeholder_text("Mi API")
        self.carpeta = Gtk.FileChooserButton(title="Carpeta del proyecto", action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.carpeta.set_filename(os.path.expanduser(datos.get("carpeta", str(HOME / "Proyectos"))))
        self.comando = Gtk.Entry(text=datos.get("comando", ""), activates_default=True)
        self.comando.set_placeholder_text("python3 app.py   ·   node server.js   ·   npm run dev")
        self.puerto = Gtk.Entry(text=str(datos.get("puerto") or ""), activates_default=True, width_chars=8)
        self.puerto.set_placeholder_text("opcional")
        self.variables = Gtk.TextView(monospace=True, top_margin=4, bottom_margin=4, left_margin=6)
        self.variables.get_buffer().set_text(cfg.pares_a_texto(datos.get("variables") or {}))
        sc_var = Gtk.ScrolledWindow(min_content_height=80)
        sc_var.set_shadow_type(Gtk.ShadowType.IN)
        sc_var.add(self.variables)
        sc_var.set_tooltip_text("Variables de entorno, una por línea: CLAVE=valor")
        for i, (lab, w) in enumerate((("Nombre", self.nombre), ("Carpeta", self.carpeta),
                                      ("Comando", self.comando), ("Puerto", self.puerto),
                                      ("Variables", sc_var)), start=1):
            g.attach(Gtk.Label(label=lab, xalign=1, yalign=0 if w is sc_var else 0.5), 0, i, 1, 1)
            g.attach(w, 1, i, 1, 1)
        ayuda = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        ayuda.set_markup('<span size="small" alpha="65%">El comando se ejecuta dentro de la carpeta, como en una '
                         'terminal (admite <tt>source .venv/bin/activate &amp;&amp; …</tt>). Con puerto, el panel '
                         'lo reconoce por el puerto y añade el botón Abrir; sin puerto, por la carpeta y el comando. '
                         'La salida se guarda en un log.</span>')
        g.attach(ayuda, 0, 6, 2, 1)
        self.error = Gtk.Label(xalign=0)
        g.attach(self.error, 0, 7, 2, 1)
        self.show_all()

    def on_elegir(self, _b):
        fc = Gtk.FileChooserDialog(title="Elegir script", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        fc.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Elegir", Gtk.ResponseType.OK)
        fc.set_current_folder(self.carpeta.get_filename() or str(HOME))
        f = Gtk.FileFilter()
        f.set_name("Scripts (.py, .js, .ts, .sh, package.json)")
        for pat in ("*.py", "*.js", "*.mjs", "*.cjs", "*.ts", "*.sh", "package.json"):
            f.add_pattern(pat)
        fc.add_filter(f)
        todo = Gtk.FileFilter()
        todo.set_name("Todos los archivos")
        todo.add_pattern("*")
        fc.add_filter(todo)
        if fc.run() == Gtk.ResponseType.OK:
            arch = fc.get_filename()
            carpeta = os.path.dirname(arch)
            self.carpeta.set_filename(carpeta)
            self.comando.set_text(comando_para(arch))
            if not self.nombre.get_text().strip():
                base = os.path.basename(carpeta) if arch.endswith("package.json") else os.path.basename(arch)
                self.nombre.set_text(base)
        fc.destroy()

    def valores(self):
        """Devuelve el dict del servicio, o None (y muestra el error) si falta algo."""
        d = {"nombre": self.nombre.get_text().strip(), "carpeta": corto(self.carpeta.get_filename() or ""),
             "comando": self.comando.get_text().strip(), "puerto": self.puerto.get_text().strip()}
        b = self.variables.get_buffer()
        variables = cfg.texto_a_pares(b.get_text(b.get_start_iter(), b.get_end_iter(), False))
        falta = [k for k in ("nombre", "carpeta", "comando") if not d[k]]
        if falta:
            self.error.set_markup(f'<span foreground="#c53030">Falta: {", ".join(falta)}</span>')
            return None
        if d["puerto"] and not d["puerto"].isdigit():
            self.error.set_markup('<span foreground="#c53030">El puerto tiene que ser un número</span>')
            return None
        d["puerto"] = int(d["puerto"]) if d["puerto"] else None
        if variables:
            d["variables"] = variables
        return d


MARCAS_LOG = (("error", re.compile(r"error|fail|fatal|traceback|exception|critical|panic|✗", re.I)),
               ("aviso", re.compile(r"warn", re.I)))


class VisorLog(Gtk.Window):
    """Ventana con el final de un log. Con "Seguir" se relee cada 2 s y baja sola al final."""

    def __init__(self, fuente):
        super().__init__(title="Logs · " + fuente["titulo"])
        self.fuente, self.texto, self.leyendo = fuente, None, False
        self.set_default_size(1000, 600)

        hb = Gtk.HeaderBar(title="Logs · " + fuente["titulo"], show_close_button=True)
        hb.set_subtitle(corto(fuente["archivo"]) if "archivo" in fuente else " ".join(fuente["cmd"]))
        self.seguir = Gtk.ToggleButton(label="Seguir", active=True)
        self.seguir.set_tooltip_text("Actualizar cada 2 segundos y bajar al final")
        hb.pack_start(self.seguir)
        b = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        b.set_tooltip_text("Leer ahora")
        b.connect("clicked", lambda *_: self.recargar(forzar=True))
        hb.pack_start(b)
        if "archivo" in fuente:
            b = Gtk.Button(label="Abrir archivo")
            b.set_tooltip_text("Abrirlo en el editor de texto")
            b.connect("clicked", lambda *_: run(["xdg-open", str(fuente["archivo"])]))
            hb.pack_end(b)
        b = Gtk.Button(label="Copiar")
        b.set_tooltip_text("Copiar lo que se ve al portapapeles")
        b.connect("clicked", self.on_copiar)
        hb.pack_end(b)
        self.set_titlebar(hb)

        caja = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.filtro = Gtk.SearchEntry(placeholder_text="Filtrar líneas (p. ej. error)", margin=6)
        self.filtro.connect("search-changed", lambda *_: self.pintar())
        caja.pack_start(self.filtro, False, False, 0)
        self.vista = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8, right_margin=8)
        buf = self.vista.get_buffer()
        buf.create_tag("error", foreground="#e5484d")
        buf.create_tag("aviso", foreground="#d39b2a")
        self.sc = Gtk.ScrolledWindow(vexpand=True)
        self.sc.add(self.vista)
        caja.pack_start(self.sc, True, True, 0)
        self.add(caja)

        self.recargar(forzar=True)
        self.temporizador = GLib.timeout_add_seconds(2, self.on_tic)
        self.connect("destroy", lambda *_: GLib.source_remove(self.temporizador))
        self.show_all()

    def on_tic(self):
        if self.seguir.get_active():
            self.recargar()
        return True

    def recargar(self, forzar=False):
        if self.leyendo:
            return
        self.leyendo = True

        def trabajo():
            texto = leer_log(self.fuente)
            GLib.idle_add(fin, texto)

        def fin(texto):
            self.leyendo = False
            if forzar or texto != self.texto:
                self.texto = texto
                self.pintar()
            return False

        threading.Thread(target=trabajo, daemon=True).start()

    def pintar(self):
        lineas = (self.texto or "").splitlines()
        f = self.filtro.get_text().strip().lower()
        if f:
            lineas = [l for l in lineas if f in l.lower()] or [f"(Ninguna línea contiene «{f}»)"]
        buf = self.vista.get_buffer()
        buf.set_text("\n".join(lineas))
        for i, l in enumerate(lineas):
            for tag, rx in MARCAS_LOG:
                if rx.search(l):
                    buf.apply_tag_by_name(tag, buf.get_iter_at_line(i), buf.get_iter_at_line_offset(i, len(l)))
                    break
        if self.seguir.get_active():
            # al final de verdad cuando el TextView ya ha calculado la altura del texto nuevo
            GLib.idle_add(lambda: self.vista.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 1) and False)

    def on_copiar(self, _b):
        buf = self.vista.get_buffer()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
            buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False), -1)


class Panel(Gtk.Window):
    def __init__(self):
        super().__init__(title="Panel de servicios")
        # que quepa en la pantalla más pequeña (la del portátil): si es más alta que el área útil, Muffin la
        # recoloca y la lista de ventanas de Cinnamon no la asocia al lanzador (sale con el icono genérico)
        disp = Gdk.Display.get_default()
        util = [disp.get_monitor(i).get_workarea() for i in range(disp.get_n_monitors())]
        # (-120: el tamaño no incluye la barra de título ni la sombra de la ventana, que suman unos 100 px)
        self.set_default_size(min([880] + [w.width - 120 for w in util]), min([760] + [w.height - 120 for w in util]))
        # por nombre del tema (~/.local/share/icons/hicolor): con la ruta del .svg, la lista de ventanas de
        # Cinnamon enseña el icono de "imagen no encontrada"
        if Gtk.IconTheme.get_default().has_icon("panel-servicios"):
            Gtk.Window.set_default_icon_name("panel-servicios")
        else:
            Gtk.Window.set_default_icon_from_file(str(Path(__file__).with_name("icono.svg")))
        self.visores = {}
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(self.get_screen(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        hb = Gtk.HeaderBar(title="Panel de servicios", show_close_button=True)
        self.sub = "Arrancar y parar lo que tengo en marcha"
        hb.set_subtitle(self.sub)
        b = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        b.set_tooltip_text("Comprobar ahora")
        b.connect("clicked", lambda *_: self.refrescar())
        hb.pack_end(b)
        b = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
        b.set_tooltip_text("Ajustes del panel")
        b.connect("clicked", lambda *_: self.configurar(cfg.ConfigAjustes(
            "Panel de servicios", ["refresco_s", "ollama_url", "creador_dir", "creador_puerto"], self.aplicar_ajustes)))
        hb.pack_end(b)
        add = Gtk.Button(label="➕ Añadir servicio")
        add.connect("clicked", lambda *_: self.dialogo_servicio())
        hb.pack_start(add)
        self.set_titlebar(hb)
        self.hb = hb

        self.caja = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=16)
        sc = Gtk.ScrolledWindow()
        sc.add(self.caja)
        self.add(sc)

        self.gateway = SystemdUser("Gateway de OpenClaw", "openclaw-gateway.service",
                                   "127.0.0.1:18789 · panel, Telegram, tareas programadas")
        self.limpieza = SystemdUser("Limpieza del registro de llamadas", "openclaw-limpiar-registro.timer",
                                    "Recorta cache-trace.jsonl cada cierto tiempo")
        self.creador = Creador()
        self.docker = SystemdSistema("Docker (servicio)", ["docker.socket", "docker.service"],
                                     "Motor de contenedores · cambiarlo pide tu contraseña")
        self.ollama_modelos = []

        self.seccion("🦞 OpenClaw")
        lb = self.lista()
        self.f_gateway = self.fila(lb, self.gateway.nombre, self.gateway,
                                   opciones=[self.op_config(cfg.config_gateway)])
        self.f_creador = self.fila(lb, self.creador.nombre, self.creador, opciones=[
            self.op_config(lambda: cfg.ConfigAjustes(self.creador.nombre, ["creador_dir", "creador_puerto"],
                                                     self.aplicar_ajustes)),
            ("web-browser-symbolic", "Abrir en el navegador", self.creador.abrir, True)])
        self.f_limpieza = self.fila(lb, self.limpieza.nombre, self.limpieza,
                                    opciones=[self.op_config(cfg.config_limpieza)])

        self.seccion("🧩 Mis servicios")
        self.lb_reg = self.lista()
        self.filas_reg = {}
        self.vacio_reg = self.vacio(self.lb_reg, "Aún no has añadido ninguno. Pulsa ➕ Añadir servicio, o "
                                                 "Guardar en Mis servicios en el engranaje ⚙ de uno de los detectados.")

        self.seccion("🔌 Otros servidores detectados")
        self.lb_det = self.lista()
        self.filas_det = {}
        self.vacio_det = self.vacio(self.lb_det, "Ningún otro programa de desarrollo (Python, Node…) escuchando en "
                                                 "un puerto.")

        self.seccion("🦙 Ollama (otro ordenador)")
        lb = self.lista()
        self.f_ollama = self.fila(lb, "Ollama", None, controlable=False,
                                  opciones=[self.op_config(lambda: cfg.ConfigAjustes("Ollama", ["ollama_url"],
                                                                                     self.aplicar_ajustes)),
                                            ("system-run-symbolic", "Liberar GPU (descargar los modelos de memoria)",
                                             lambda: ollama_liberar(self.ollama_modelos), True)])

        self.seccion("🐳 Docker")
        self.lb_docker = self.lista()
        self.f_docker = self.fila(self.lb_docker, self.docker.nombre, self.docker,
                                  opciones=[self.op_config(cfg.ConfigDocker)])
        self.filas_cont = {}

        nota = Gtk.Label(xalign=0, wrap=True)
        nota.set_markup(f'<span size="small" alpha="65%">Se comprueba cada {REFRESCO_S} segundos. <span foreground="#1f8a4c">●</span> en marcha · '
                        '<span foreground="#c53030">●</span> parado. El interruptor de la izquierda arranca o para '
                        'cada servicio; el engranaje de la derecha tiene su configuración '
                        '(arrancar al encender, logs, editar…). Tus servicios se guardan en '
                        f'{corto(CONFIG)}.</span>')
        nota.set_margin_top(14)
        self.caja.pack_start(nota, False, False, 0)

        self.registrados = cargar_registrados()
        self.ocupados = set()
        self.refrescando = False
        self.refrescar()
        self.temporizador = GLib.timeout_add_seconds(REFRESCO_S, lambda: (self.refrescar(), True)[1])

    def seccion(self, texto):
        l = Gtk.Label(label=texto, xalign=0)
        l.get_style_context().add_class("seccion")
        self.caja.pack_start(l, False, False, 0)

    def lista(self):
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        fr = Gtk.Frame()
        fr.add(lb)
        self.caja.pack_start(fr, False, False, 0)
        return lb

    def vacio(self, lb, texto):
        row = Gtk.ListBoxRow(activatable=False)
        l = Gtk.Label(label=texto, xalign=0, wrap=True)
        l.get_style_context().add_class("vacio")
        row.add(l)
        lb.add(row)
        return row

    def fila(self, lb, nombre, servicio, opciones=(), controlable=True):
        f = Fila(self, nombre, servicio, opciones, controlable)
        lb.add(f)
        return f

    # --- comprobaciones en segundo plano ---
    def refrescar(self):
        if self.refrescando:
            return
        self.refrescando = True
        regs = [Registrado(d) for d in self.registrados]
        threading.Thread(target=self._comprobar, args=(regs,), daemon=True).start()

    def _comprobar(self, regs):
        try:
            mapa = puertos_escuchando()
            reg_est = []
            conocidos = {os.getpid()}
            for r in regs:
                est = r.estado(mapa)
                reg_est.append((r, est))
                pid = r.buscar_pid(mapa)
                if pid:
                    conocidos.add(pid)
            for port in (GATEWAY_PORT, CREADOR_PORT):
                pid = pid_en_puerto(port, mapa)
                if pid:
                    conocidos.add(pid)
            det = []
            for pid, puertos in mapa.items():
                inf = info_proceso(pid)
                # el nombre del ejecutable, no el del proceso (node se llama a sí mismo "MainThread")
                if pid in conocidos or not inf or not DETECTAR.match(Path(inf[1][0]).name if inf[1] else inf[0]):
                    continue
                if any(pid_padre(pid) == c for c in conocidos):  # hijo de uno ya listado (p. ej. npm → node)
                    continue
                det.append(Detectado(pid, *inf, puertos))
            datos = {
                "gateway": self.gateway.estado(),
                "creador": self.creador.estado(mapa),
                "limpieza": self.limpieza.estado(),
                "docker": self.docker.estado(),
                "ollama": ollama_estado(),
                "conts": contenedores(),
                "regs": reg_est,
                "det": det,
            }
        except Exception as e:  # nunca dejar el panel colgado
            datos = {"error": str(e)}
        GLib.idle_add(self._pintar, datos)

    def _sincronizar(self, lb, filas, items, crear):
        """Añade, actualiza y quita filas de una lista dinámica. items: [(clave, servicio, estado)]."""
        vistos = set()
        for clave, serv, est in items:
            vistos.add(clave)
            f = filas.get(clave)
            if f is None:
                f = crear(serv)
                f.show_all()
                filas[clave] = f
            if f not in self.ocupados:
                f.servicio = serv
                f.actualizar(est)
        for k in list(filas):
            if k not in vistos:
                lb.remove(filas.pop(k))

    def _pintar(self, d):
        self.refrescando = False
        if "error" in d:
            self.hb.set_subtitle("Error al comprobar: " + d["error"])
            return False
        self.hb.set_subtitle(self.sub)
        for f, k in ((self.f_gateway, "gateway"), (self.f_creador, "creador"), (self.f_limpieza, "limpieza"),
                     (self.f_docker, "docker"), (self.f_ollama, "ollama")):
            if f not in self.ocupados:
                f.actualizar(d[k])
        self.ollama_modelos = d["ollama"]["modelos"]

        # mis servicios: la clave incluye los datos, así al editarlo se rehace la fila
        self._sincronizar(self.lb_reg, self.filas_reg,
                          [(json.dumps(r.d, sort_keys=True), r, e) for r, e in d["regs"]], self._fila_registrado)
        self.vacio_reg.set_visible(not self.filas_reg)

        self._sincronizar(self.lb_det, self.filas_det,
                          [((x.pid, tuple(x.puertos)), x, x.estado()) for x in d["det"]], self._fila_detectado)
        self.vacio_det.set_visible(not self.filas_det)

        conts = d["conts"] or []
        self._sincronizar(self.lb_docker, self.filas_cont, [(c.nombre, c, e) for c, e in conts],
                          lambda c: self.fila(self.lb_docker, "Contenedor " + c.nombre
                                              + (f"  ·  {c.proyecto}" if c.proyecto else ""), c,
                                              opciones=[self.op_config(lambda: cfg.ConfigContenedor(c.nombre))]))
        return False

    def _fila_registrado(self, r):
        opciones = [("web-browser-symbolic", "Abrir en el navegador", r.abrir, True)] if r.puerto else []
        opciones.insert(0, ("emblem-system-symbolic", "Configuración…", lambda: self.dialogo_servicio(r.d), False))
        opciones += [
                     ("user-trash-symbolic", "Quitar de la lista", lambda: self.quitar(r.d), False)]
        return self.fila(self.lb_reg, r.nombre, r, opciones=opciones)

    def _fila_detectado(self, x):
        opciones = [("web-browser-symbolic", "Abrir en el navegador", x.abrir, True),
                    ("document-save-symbolic", "Guardar en Mis servicios…", lambda: self.guardar_detectado(x), False)]
        return self.fila(self.lb_det, x.nombre, x, opciones=opciones)

    # --- configuración ---
    def op_config(self, crear):
        """Opción «Configuración…» del engranaje: crear() devuelve el objeto de configuracion.py."""
        return ("emblem-system-symbolic", "Configuración…", lambda: self.configurar(crear()), False)

    def configurar(self, c):
        cfg.DialogoConfig(self, c, al_terminar=self.refrescar)

    def aplicar_ajustes(self, cambios):
        """Tras guardar ajustes.json (en el hilo de aplicar): usar los valores nuevos ya, sin reiniciar el panel."""
        global CREADOR_DIR, CREADOR_PORT, OLLAMA, REFRESCO_S
        a = cfg.leer_ajustes()
        reiniciar = ({"creador_dir", "creador_puerto"} & set(cambios)) and pid_en_puerto(CREADOR_PORT)
        if reiniciar:
            self.creador.parar()  # con el puerto viejo
        CREADOR_DIR = Path(os.path.expanduser(a["creador_dir"]))
        CREADOR_PORT = int(a["creador_puerto"])
        OLLAMA = a["ollama_url"].rstrip("/")
        if "refresco_s" in cambios:
            REFRESCO_S = int(a["refresco_s"])

            def nuevo_temporizador():
                GLib.source_remove(self.temporizador)
                self.temporizador = GLib.timeout_add_seconds(REFRESCO_S, lambda: (self.refrescar(), True)[1])
            GLib.idle_add(nuevo_temporizador)
        if reiniciar:
            ok, msg = self.creador.arrancar()
            return (True, f"El Creador se ha reiniciado en http://127.0.0.1:{CREADOR_PORT}") if ok else (False, msg)
        return True, ""

    # --- mis servicios ---
    def dialogo_servicio(self, datos=None):
        dlg = DialogoServicio(self, dict(datos, _editar=True) if datos and datos in self.registrados else datos)
        while dlg.run() == Gtk.ResponseType.OK:
            nuevo = dlg.valores()
            if nuevo is None:
                continue
            otros = [s for s in self.registrados if s is not datos]
            if any(s["nombre"] == nuevo["nombre"] for s in otros):
                dlg.error.set_markup('<span foreground="#c53030">Ya hay un servicio con ese nombre</span>')
                continue
            if datos in self.registrados:
                self.registrados[self.registrados.index(datos)] = nuevo
            else:
                self.registrados.append(nuevo)
            guardar_registrados(self.registrados)
            break
        dlg.destroy()
        self.refrescar()

    def guardar_detectado(self, x):
        cmd = shlex.join([Path(x.args[0]).name] + x.args[1:]) if x.args else x.comm
        nombre = next((Path(a).name for a in reversed(x.args) if "." in Path(a).name), x.comm)
        self.dialogo_servicio({"nombre": nombre, "carpeta": corto(x.cwd), "comando": cmd,
                               "puerto": x.puertos[0] if x.puertos else None})
        return False

    def quitar(self, datos):
        dlg = Gtk.MessageDialog(transient_for=self, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO, text=f"¿Quitar «{datos['nombre']}» de la lista?")
        dlg.format_secondary_text("Solo se quita del panel: no se para ni se borra nada de su carpeta.")
        if dlg.run() == Gtk.ResponseType.YES and datos in self.registrados:
            self.registrados.remove(datos)
            guardar_registrados(self.registrados)
        dlg.destroy()
        self.refrescar()

    def ver_log(self, fuente):
        """Abre el visor de ese log, o trae al frente el que ya esté abierto."""
        clave = str(fuente.get("archivo") or fuente["cmd"])
        v = self.visores.get(clave)
        if v is not None:
            v.present()
            return
        v = VisorLog(fuente)
        self.visores[clave] = v
        v.connect("destroy", lambda *_: self.visores.pop(clave, None))

    # --- acciones ---
    def accion(self, fila, fn):
        if fila is not None:
            self.ocupados.add(fila)
            fila.ocupado(True)

        def trabajo():
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, str(e)
            GLib.idle_add(fin, ok, msg)

        def fin(ok, msg):
            if fila is not None:
                self.ocupados.discard(fila)
                fila.ocupado(False)
            if not ok and msg and "dismissed" not in msg.lower():
                dlg = Gtk.MessageDialog(transient_for=self, message_type=Gtk.MessageType.WARNING,
                                        buttons=Gtk.ButtonsType.CLOSE, text="No se ha podido completar")
                dlg.format_secondary_text(msg[-1200:])
                dlg.run()
                dlg.destroy()
            self.refrescar()
            return False

        threading.Thread(target=trabajo, daemon=True).start()


def pid_padre(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def main():
    # mismo nombre que el lanzador (panel-servicios.desktop): así Cinnamon asocia la ventana con su icono
    GLib.set_prgname("panel-servicios")
    GLib.set_application_name("Panel de servicios")
    Gdk.set_program_class("panel-servicios")
    win = Panel()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.vacio_reg.set_visible(not win.filas_reg)
    win.vacio_det.set_visible(not win.filas_det)
    Gtk.main()


if __name__ == "__main__":
    main()
