#!/usr/bin/env python3
"""Panel de servicios en la terminal: lo mismo que panel.py, pero para usarlo por SSH.

Reutiliza las clases y comprobaciones de panel.py (que se puede importar sin pantalla: solo define las
ventanas, no las abre). La interfaz es de Textual. Las comprobaciones y acciones van en hilos aparte y la
tabla se actualiza con call_from_thread, así la terminal nunca se queda bloqueada.

Se lanza con el comando `servicios` (~/.local/bin/servicios), que usa el entorno .venv de esta carpeta.
«Mis servicios» se añaden, cambian y quitan aquí también (a, m, Supr): van a ~/.config/panel-servicios/servicios.json.
"""
import json
import os
import shlex
import subprocess
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static, TextArea

import panel as p

EDITOR_REMOTO = "/.vscode-server/"
VERDE, ROJO, GRIS = "#3fb950", "#f85149", "#8b949e"


def existe_unidad(unidad, usuario=True):
    """Si systemd conoce la unidad: lo que no está instalado en este equipo no se enseña."""
    return p.run(["systemctl", *(["--user"] if usuario else []), "cat", unidad], timeout=5)[0]


def ollama_configurado():
    """Ollama solo se enseña si se ha puesto su dirección en ajustes.json (o responde en la de serie)."""
    try:
        return "ollama_url" in json.loads(p.cfg.AJUSTES.read_text())
    except (OSError, ValueError):
        return False


def comprobar(gateway, creador, limpieza, docker, hay):
    """Lo mismo que Panel._comprobar de panel.py: el estado de todo, en una lista de secciones."""
    mapa = p.puertos_escuchando()
    regs, conocidos = [], {os.getpid()}
    for d in p.cargar_registrados():
        r = p.Registrado(d)
        regs.append((r, r.estado(mapa)))
        pid = r.buscar_pid(mapa)
        if pid:
            conocidos.add(pid)
    for port in (p.GATEWAY_PORT, p.CREADOR_PORT):
        pid = p.pid_en_puerto(port, mapa)
        if pid:
            conocidos.add(pid)
    det = []
    for pid, puertos in mapa.items():
        inf = p.info_proceso(pid)
        if pid in conocidos or not inf or not p.DETECTAR.match(Path(inf[1][0]).name if inf[1] else inf[0]):
            continue
        if any(p.pid_padre(pid) == c for c in conocidos):
            continue
        if any(EDITOR_REMOTO in a for a in inf[1]):  # el servidor de VS Code al conectarse por SSH
            continue
        x = p.Detectado(pid, *inf, puertos)
        det.append((x, x.estado()))
    ollama = p.ollama_estado()
    if not ollama["on"] and not hay["ollama"]:
        ollama = None
    conts = p.contenedores() if hay["docker"] else []
    openclaw = [(s, e()) for s, e, si in ((gateway, gateway.estado, hay["gateway"]),
                                           (creador, lambda: creador.estado(mapa), p.CREADOR_DIR.joinpath("app.py").exists()),
                                           (limpieza, limpieza.estado, hay["limpieza"])) if si]
    return [
        ("🦞 OpenClaw", openclaw, ""),
        ("🧩 Mis servicios", regs, "Aún no has añadido ninguno: pulsa «a» para añadir uno"),
        ("🔌 Otros servidores detectados", det, "Ningún otro programa de desarrollo escuchando en un puerto"),
        ("🦙 Ollama", [("ollama", ollama)] if ollama else [], ""),
        ("🐳 Docker", ([(docker, docker.estado())] + [(c, e) for c, e in (conts or [])]) if hay["docker"] else [],
         "" if conts is not None else "Docker no responde"),
    ], (ollama or {}).get("modelos", [])


def nombre_de(serv):
    if serv == "ollama":
        return "Ollama"
    if isinstance(serv, p.Contenedor):
        return "Contenedor " + serv.nombre + (f"  ·  {serv.proyecto}" if serv.proyecto else "")
    return serv.nombre


def clave_de(serv):
    if serv == "ollama":
        return "ollama"
    if isinstance(serv, p.Detectado):
        return f"det:{serv.pid}"
    return f"{type(serv).__name__}:{serv.nombre}"


def guardar_servicio(nuevo, original=None):
    """Añade un servicio a servicios.json o cambia el que se llamaba `original`. Devuelve un error o None.

    Se relee el archivo antes de escribir: así no se pisa lo que se haya cambiado desde otro sitio mientras tanto."""
    lista = p.cargar_registrados()
    if any(s["nombre"] == nuevo["nombre"] and s["nombre"] != original for s in lista):
        return "Ya hay un servicio con ese nombre"
    i = next((i for i, s in enumerate(lista) if original is not None and s["nombre"] == original), None)
    if i is None:
        lista.append(nuevo)
    else:
        lista[i] = nuevo
    p.guardar_registrados(lista)
    return None


def quitar_servicio(nombre):
    p.guardar_registrados([s for s in p.cargar_registrados() if s["nombre"] != nombre])


def datos_de_detectado(x):
    """Lo que se rellena al guardar en «Mis servicios» un servidor detectado (como guardar_detectado de panel.py)."""
    cmd = shlex.join([Path(x.args[0]).name] + x.args[1:]) if x.args else x.comm
    nombre = next((Path(a).name for a in reversed(x.args) if "." in Path(a).name), x.comm)
    return {"nombre": nombre, "carpeta": p.corto(x.cwd), "comando": cmd,
            "puerto": x.puertos[0] if x.puertos else None}


class FormServicio(ModalScreen):
    """Añadir o editar uno de «Mis servicios»: los mismos campos que DialogoServicio de panel.py."""

    BINDINGS = [Binding("escape", "cancelar", "Cancelar"), Binding("ctrl+s", "guardar", "Guardar", priority=True)]

    def __init__(self, datos=None, original=None):
        super().__init__()
        self.datos, self.original = datos or {}, original  # original: nombre del servicio que se edita

    def compose(self) -> ComposeResult:
        d = self.datos
        with Vertical(id="form"):
            yield Static(f"[b]{'Editar' if self.original else 'Añadir'} servicio[/b]")
            yield Label("Nombre")
            yield Input(d.get("nombre", ""), placeholder="Mi API", id="nombre")
            yield Label("Carpeta (o la ruta de un script: se rellenan solos carpeta, comando y nombre)")
            yield Input(d.get("carpeta", "~/Proyectos"), id="carpeta")
            yield Label("Comando")
            yield Input(d.get("comando", ""), placeholder="python3 app.py   ·   node server.js   ·   npm run dev",
                        id="comando")
            yield Label("Puerto (opcional)")
            yield Input(str(d.get("puerto") or ""), placeholder="8000", type="integer", id="puerto")
            yield Label("Variables de entorno (opcional, una por línea: CLAVE=valor)")
            yield TextArea(p.cfg.pares_a_texto(d.get("variables") or {}), id="variables")
            yield Static("[dim]El comando se ejecuta dentro de la carpeta, como en una terminal. Con puerto, se "
                         "reconoce por el puerto; sin él, por la carpeta y el comando.[/dim]")
            yield Static("", id="form-error")
            with Horizontal(id="form-botones"):
                yield Button("Guardar (Ctrl+S)", variant="primary", id="guardar")
                yield Button("Cancelar (Esc)", id="cancelar")

    def valores(self):
        """El dict del servicio, o un texto con lo que falta o está mal."""
        v = {k: self.query_one(f"#{k}", Input).value.strip() for k in ("nombre", "carpeta", "comando", "puerto")}
        ruta = os.path.expanduser(v["carpeta"])
        if os.path.isfile(ruta):  # han puesto un script: como «Elegir script…» del panel de escritorio
            carpeta = os.path.dirname(os.path.abspath(ruta))
            v["comando"] = v["comando"] or p.comando_para(ruta)
            v["nombre"] = v["nombre"] or (os.path.basename(carpeta) if ruta.endswith("package.json")
                                          else os.path.basename(ruta))
            ruta = carpeta
        falta = [k for k in ("nombre", "carpeta", "comando") if not v[k]]
        if falta:
            return "Falta: " + ", ".join(falta)
        if not os.path.isdir(ruta):
            return f"La carpeta no existe: {ruta}"
        if v["puerto"] and not v["puerto"].isdigit():
            return "El puerto tiene que ser un número"
        d = {"nombre": v["nombre"], "carpeta": p.corto(os.path.abspath(ruta)), "comando": v["comando"],
             "puerto": int(v["puerto"]) if v["puerto"] else None}
        variables = p.cfg.texto_a_pares(self.query_one("#variables", TextArea).text)
        if variables:
            d["variables"] = variables
        return d

    def action_guardar(self):
        d = self.valores()
        error = d if isinstance(d, str) else guardar_servicio(d, self.original)
        if error:
            self.query_one("#form-error", Static).update(f"[{ROJO}]{error}[/]")
            return
        self.dismiss(d)

    def action_cancelar(self):
        self.dismiss(None)

    @on(Input.Submitted)
    def enter(self):
        self.action_guardar()

    @on(Button.Pressed, "#guardar")
    def boton_guardar(self):
        self.action_guardar()

    @on(Button.Pressed, "#cancelar")
    def boton_cancelar(self):
        self.action_cancelar()


class Confirmar(ModalScreen):
    """Pregunta de sí o no: «s» o Enter = sí; «n» o Esc = no."""

    BINDINGS = [Binding("s,enter", "si", "Sí"), Binding("n,escape", "no", "No")]

    def __init__(self, pregunta, detalle=""):
        super().__init__()
        self.pregunta, self.detalle = pregunta, detalle

    def compose(self) -> ComposeResult:
        with Vertical(id="aviso"):
            yield Static(f"[b]{self.pregunta}[/b]", id="aviso-titulo")
            yield Static(self.detalle)
            yield Static("[dim]s = sí · n = no[/dim]")

    def action_si(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class Aviso(ModalScreen):
    """Mensaje de error de una acción; se cierra con cualquier tecla."""

    def __init__(self, titulo, texto):
        super().__init__()
        self.titulo, self.texto = titulo, texto

    def compose(self) -> ComposeResult:
        with Vertical(id="aviso"):
            yield Static(f"[b]{self.titulo}[/b]", id="aviso-titulo")
            yield Static(Text(self.texto[-1500:]))
            yield Static("[dim]Pulsa cualquier tecla para cerrar[/dim]")

    def on_key(self, event):
        event.stop()
        self.dismiss()


class VisorLog(Screen):
    """Últimas líneas del log de un servicio, actualizado cada 2 s (como el visor de panel.py)."""

    BINDINGS = [
        Binding("escape,q", "volver", "Volver"),
        Binding("slash", "filtrar", "Filtrar", priority=True),
        Binding("f", "seguir", "Seguir el final"),
    ]

    def __init__(self, fuente):
        super().__init__()
        self.fuente, self.filtro, self.texto, self.seguir = fuente, "", None, True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Filtrar líneas (Enter para aplicar, vacío para quitar el filtro)", id="filtro")
        yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self):
        self.title = "Log · " + self.fuente["titulo"]
        self.query_one("#filtro").display = False
        self.query_one(RichLog).focus()  # si no, el foco empieza en el filtro (aunque esté oculto)
        self.recargar()
        self.set_interval(2, self.recargar)

    @work(thread=True, exclusive=True, group="log")
    def recargar(self):
        texto = p.leer_log(self.fuente)
        if texto != self.texto:
            self.app.call_from_thread(self.pintar, texto)

    def pintar(self, texto):
        self.texto = texto
        log = self.query_one(RichLog)
        log.clear()
        for linea in texto.splitlines():
            if self.filtro and self.filtro.lower() not in linea.lower():
                continue
            t = Text(linea)
            for marca, rx in p.MARCAS_LOG:
                if rx.search(linea):
                    t.stylize(ROJO if marca == "error" else "#d29922")
                    break
            log.write(t, scroll_end=self.seguir)

    def action_volver(self):
        if self.query_one("#filtro").has_focus:
            self.query_one("#filtro").display = False
            self.query_one(RichLog).focus()
        else:
            self.app.pop_screen()

    def action_filtrar(self):
        # el atajo tiene prioridad (si no, la «/» que abre el filtro se escribe en él): dentro del filtro, escribe «/»
        f = self.query_one("#filtro")
        if f.has_focus:
            f.insert_text_at_cursor("/")
            return
        f.display = True
        f.focus()

    def action_seguir(self):
        self.seguir = not self.seguir
        self.notify("Siguiendo el final del log" if self.seguir else "Ya no sigue el final")
        if self.seguir:
            self.query_one(RichLog).scroll_end(animate=False)

    @on(Input.Submitted, "#filtro")
    def aplicar_filtro(self, ev):
        self.filtro = ev.value.strip()
        self.sub_title = f"filtro: {self.filtro}" if self.filtro else ""
        if self.texto is not None:
            self.pintar(self.texto)
        self.query_one(RichLog).focus()


class Tabla(DataTable):
    """La tabla se queda Enter para «seleccionar fila»: aquí Enter arranca o para (un clic solo mueve el cursor)."""

    BINDINGS = [Binding("enter", "app.alternar", "Arrancar/Parar", show=False)]


class PanelTerminal(App):
    TITLE = "Panel de servicios"
    SUB_TITLE = "Arrancar y parar lo que tengo en marcha"
    CSS = """
    DataTable { height: 1fr; }
    #ayuda { height: auto; padding: 0 1; color: $text-muted; }
    #aviso { width: 80%; max-width: 100; height: auto; padding: 1 2; background: $panel;
             border: thick $error; }
    #aviso-titulo { padding-bottom: 1; }
    Aviso, Confirmar, FormServicio { align: center middle; }
    #form { width: 90%; max-width: 100; height: auto; max-height: 100%; padding: 1 2; background: $panel;
            border: thick $primary; overflow-y: auto; }
    #form Label { margin-top: 1; }
    #variables { height: 5; }
    #form-error { margin-top: 1; }
    #form-botones { height: auto; margin-top: 1; }
    #form-botones Button { margin-right: 2; }
    """
    BINDINGS = [
        Binding("space", "alternar", "Arrancar/Parar"),
        Binding("l", "logs", "Logs"),
        Binding("a", "anadir", "Añadir"),
        Binding("m", "editar", "Editar"),
        Binding("delete", "quitar", "Quitar"),
        Binding("e", "inicio", "Al encender"),
        Binding("g", "liberar", "Liberar GPU"),
        Binding("r", "refrescar", "Comprobar"),
        Binding("q", "quit", "Salir"),
    ]

    def __init__(self):
        super().__init__()
        self.gateway = p.SystemdUser("Gateway de OpenClaw", "openclaw-gateway.service",
                                     "127.0.0.1:18789 · panel, Telegram, tareas programadas")
        self.limpieza = p.SystemdUser("Limpieza del registro de llamadas", "openclaw-limpiar-registro.timer",
                                      "Recorta cache-trace.jsonl cada cierto tiempo")
        self.creador = p.Creador()
        self.docker = p.SystemdSistema("Docker (servicio)", ["docker.socket", "docker.service"],
                                       "Motor de contenedores · cambiarlo pide tu contraseña")
        self.filas = {}          # clave de fila -> (servicio, estado)
        self.ocupados = set()    # claves con una acción en curso
        self.modelos = []
        self.hay = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Tabla(cursor_type="row", zebra_stripes=False)
        yield Static(f"Se comprueba cada {p.REFRESCO_S} s · [{VERDE}]●[/] en marcha · [{ROJO}]●[/] parado · "
                     f"[{GRIS}]●[/] no aplica", id="ayuda")
        yield Footer()

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_column("", key="punto", width=2)
        t.add_column("Servicio", key="nombre")
        t.add_column("Al encender", key="inicio")
        t.add_column("Detalle", key="detalle")
        self.refrescar()
        self.set_interval(p.REFRESCO_S, self.refrescar)

    # --- comprobaciones ---
    @work(thread=True, exclusive=True, group="comprobar")
    def refrescar(self):
        try:
            if self.hay is None:  # qué hay instalado en este equipo: se mira una vez
                self.hay = {"gateway": existe_unidad(self.gateway.unidad),
                            "limpieza": existe_unidad(self.limpieza.unidad),
                            "docker": existe_unidad("docker.service", usuario=False),
                            "ollama": ollama_configurado()}
            secciones, modelos = comprobar(self.gateway, self.creador, self.limpieza, self.docker, self.hay)
        except Exception as e:  # nunca dejar el panel colgado
            self.call_from_thread(self.notify, f"Error al comprobar: {e}", severity="error")
            return
        self.call_from_thread(self.pintar, secciones, modelos)

    def pintar(self, secciones, modelos):
        self.modelos = modelos
        t = self.query_one(DataTable)
        actual = self.clave_actual()
        t.clear()
        self.filas = {}
        for titulo, items, vacio in secciones:
            if not items and not vacio:  # sección de algo que no hay en este equipo
                continue
            t.add_row("", Text(titulo, style="bold"), "", "", key=f"sec:{titulo}")
            if not items and vacio:
                t.add_row("", Text("  (ninguno)", style=GRIS), "", Text(vacio, style=GRIS), key=f"vacio:{titulo}")
            for serv, est in items:
                k = clave_de(serv)
                self.filas[k] = (serv, est)
                if k in self.ocupados:
                    punto = Text("⏳")
                else:
                    punto = Text("●", style=VERDE if est["on"] else ROJO if est["on"] is False else GRIS)
                inicio = {True: Text("sí", style=VERDE), False: Text("no", style=GRIS)}.get(est.get("inicio"), "")
                t.add_row(punto, "  " + nombre_de(serv), inicio, Text(est["detalle"], style=GRIS), key=k)
        claves = [r.value for r in t.rows]
        if actual in claves:
            t.move_cursor(row=claves.index(actual), animate=False)
        elif actual is None:
            t.move_cursor(row=next((i for i, k in enumerate(claves) if k in self.filas), 0), animate=False)

    def clave_actual(self):
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    def actual(self):
        k = self.clave_actual()
        return (k, *self.filas[k]) if k in self.filas else (None, None, None)

    def check_action(self, action, parameters):
        # los atajos de la lista no valen (ni se enseñan) en la pantalla de logs ni con un formulario o aviso abierto
        if action in {"alternar", "logs", "inicio", "liberar", "refrescar", "anadir", "editar", "quitar"} \
                and len(self.screen_stack) > 1:
            return False
        return True

    # --- acciones ---
    def ejecutar(self, clave, fn, titulo="No se ha podido completar"):
        """Ejecuta fn() -> (ok, msg) en un hilo, con la fila marcada como ocupada."""
        self.ocupados.add(clave)
        self.refrescar()

        def trabajo():
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, str(e)
            self.call_from_thread(fin, ok, msg)

        def fin(ok, msg):
            self.ocupados.discard(clave)
            if not ok and msg:
                self.push_screen(Aviso(titulo, msg))
            self.refrescar()

        self.run_worker(trabajo, thread=True, group="acciones")

    def con_sudo(self, args):
        """Por SSH no hay ventana de contraseña (pkexec): se suspende la interfaz y sudo la pide aquí."""
        with self.suspend():
            print(f"\n$ sudo {' '.join(args)}")
            r = subprocess.run(["sudo", *args])
        self.refresh()
        if r.returncode:
            self.push_screen(Aviso("No se ha podido completar", f"sudo {' '.join(args)} ha terminado con código "
                                                                  f"{r.returncode}"))
        self.refrescar()

    def action_alternar(self):
        k, serv, est = self.actual()
        if serv is None or k in self.ocupados:
            return
        if serv == "ollama":
            self.notify("Ollama está en otro ordenador: aquí solo se puede liberar la GPU (g)")
            return
        if isinstance(serv, p.SystemdSistema):
            orden = ["stop", *reversed(serv.unidades)] if est["on"] else ["start", *serv.unidades]
            self.con_sudo(["systemctl", *orden])
            return
        if est["on"]:
            self.notify(f"Parando {nombre_de(serv)}…")
            self.ejecutar(k, serv.parar)
        elif hasattr(serv, "arrancar"):
            self.notify(f"Arrancando {nombre_de(serv)}…")
            self.ejecutar(k, serv.arrancar)

    def action_inicio(self):
        k, serv, est = self.actual()
        if serv is None or est.get("inicio") is None or k in self.ocupados:
            self.notify("Este no tiene opción de arrancar al encender", severity="warning")
            return
        si = not est["inicio"]
        if isinstance(serv, p.SystemdSistema):
            self.con_sudo(["systemctl", "enable" if si else "disable", *serv.unidades])
        else:
            self.ejecutar(k, lambda: serv.set_inicio(si))
            self.notify(f"{nombre_de(serv)}: {'arrancará' if si else 'ya no arrancará'} al encender")

    def action_logs(self):
        k, serv, _ = self.actual()
        fuente = serv.fuente_log() if serv is not None and hasattr(serv, "fuente_log") else None
        if fuente is None:
            self.notify("Este no tiene log que se pueda ver desde aquí", severity="warning")
            return
        self.push_screen(VisorLog(fuente))

    def action_liberar(self):
        if not self.modelos:
            self.notify("Ollama no tiene ningún modelo cargado")
            return
        self.notify("Descargando de la GPU: " + ", ".join(self.modelos))
        self.ejecutar("ollama", lambda: p.ollama_liberar(self.modelos))

    def action_refrescar(self):
        self.refrescar()

    # --- mis servicios ---
    def guardado(self, d):
        if d:
            self.notify(f"«{d['nombre']}» guardado en {p.corto(p.CONFIG)}")
            self.refrescar()

    def action_anadir(self):
        _, serv, _ = self.actual()  # sobre un servidor detectado, se rellena con lo suyo
        self.push_screen(FormServicio(datos_de_detectado(serv) if isinstance(serv, p.Detectado) else None),
                         self.guardado)

    def action_editar(self):
        _, serv, _ = self.actual()
        if not isinstance(serv, p.Registrado):
            self.notify("Solo se pueden editar los de «Mis servicios» (con «a» se añade uno)", severity="warning")
            return
        self.push_screen(FormServicio(dict(serv.d), original=serv.nombre), self.guardado)

    def action_quitar(self):
        _, serv, _ = self.actual()
        if not isinstance(serv, p.Registrado):
            self.notify("Solo se pueden quitar los de «Mis servicios»", severity="warning")
            return

        def fin(si):
            if si:
                quitar_servicio(serv.nombre)
                self.notify(f"«{serv.nombre}» quitado de la lista")
                self.refrescar()

        self.push_screen(Confirmar(f"¿Quitar «{serv.nombre}» de la lista?",
                                   "Solo se quita del panel: no se para ni se borra nada de su carpeta."), fin)


if __name__ == "__main__":
    PanelTerminal().run()
