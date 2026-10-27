"""Configuración de cada servicio del panel: formularios con lo habitual y, para todo lo demás, el archivo entero.

Cada tipo de servicio tiene una clase Config* con:
  titulo
  paginas()  → [Formulario | Archivo]  (se leen al abrir el diálogo)
  aplicar(cambios, archivos) → (ok, mensaje)
      cambios:  {clave: valor nuevo} de los campos del formulario que se han tocado
      archivos: {nombre de la pestaña: texto nuevo} de los archivos que se han editado
aplicar() se ejecuta en un hilo aparte: puede tardar (docker compose up, pkexec, reiniciar un servicio).

Antes de guardar un archivo se valida (systemd-analyze verify, docker compose config, dockerd --validate) y se
deja una copia .bak-panel al lado, así un error nunca deja el servicio sin poder arrancar.
"""
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from gi.repository import GLib, Gtk, Pango

HOME = Path.home()
AJUSTES = HOME / ".config/panel-servicios/ajustes.json"   # ajustes propios del panel (Creador, Ollama, refresco)
AJUSTES_DEF = {"creador_dir": str(HOME / "Proyectos/creador-agentes"), "creador_puerto": 8790,
               "ollama_url": "http://127.0.0.1:11434", "refresco_s": 5}


def run(cmd, timeout=60, entrada=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=entrada)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def leer_ajustes():
    try:
        return {**AJUSTES_DEF, **json.loads(AJUSTES.read_text())}
    except (OSError, ValueError):
        return dict(AJUSTES_DEF)


def guardar_ajustes(a):
    AJUSTES.parent.mkdir(parents=True, exist_ok=True)
    AJUSTES.write_text(json.dumps(a, ensure_ascii=False, indent=2) + "\n")


def guardar_con_copia(ruta, texto):
    """Escribe el archivo dejando la versión anterior en <archivo>.bak-panel."""
    ruta = Path(ruta)
    if ruta.exists():
        ruta.with_name(ruta.name + ".bak-panel").write_bytes(ruta.read_bytes())
    tmp = ruta.with_name("." + ruta.name + ".panel-tmp")
    tmp.write_text(texto)
    os.replace(tmp, ruta)


# ---------- páginas del diálogo ----------

@dataclass
class Campo:
    """tipo: texto · numero · opcion · carpeta · si_no · lista (uno por línea) · pares (CLAVE=valor por línea) · info"""
    clave: str
    etiqueta: str
    tipo: str
    valor: object = ""
    ayuda: str = ""
    opciones: list = field(default_factory=list)   # [(valor, texto)] para tipo opcion


@dataclass
class Formulario:
    titulo: str
    campos: list
    aviso: str = ""


@dataclass
class Archivo:
    titulo: str
    texto: str
    ruta: str = ""
    solo_lectura: bool = False
    aviso: str = ""


def pares_a_texto(d):
    return "\n".join(f"{k}={'' if v is None else v}" for k, v in d.items())


def texto_a_pares(t):
    res = {}
    for linea in t.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        k, _, v = linea.partition("=")
        res[k.strip()] = v.strip()
    return res


class EditorCampo:
    """El widget de un campo y cómo leer su valor."""

    def __init__(self, campo, ventana):
        self.c, t = campo, campo.tipo
        if t in ("texto", "numero"):
            self.w = Gtk.Entry(text=str(campo.valor if campo.valor is not None else ""), hexpand=True)
        elif t == "opcion":
            self.w = Gtk.ComboBoxText(hexpand=True)
            ids = [str(v) for v, _ in campo.opciones]
            for v, txt in campo.opciones:
                self.w.append(str(v), txt)
            if str(campo.valor) not in ids:  # valor que no está en la lista: se respeta
                self.w.append(str(campo.valor), str(campo.valor))
            self.w.set_active_id(str(campo.valor))
        elif t == "carpeta":
            self.w = Gtk.Box(spacing=6, hexpand=True)
            self.entry = Gtk.Entry(text=str(campo.valor or ""), hexpand=True)
            b = Gtk.Button.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON)
            b.set_tooltip_text("Elegir carpeta")
            b.connect("clicked", self.elegir, ventana)
            self.w.pack_start(self.entry, True, True, 0)
            self.w.pack_start(b, False, False, 0)
        elif t == "si_no":
            self.w = Gtk.Switch(active=bool(campo.valor), halign=Gtk.Align.START)
        elif t in ("lista", "pares"):
            texto = pares_a_texto(campo.valor or {}) if t == "pares" else "\n".join(map(str, campo.valor or []))
            self.tv = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.CHAR, top_margin=4, bottom_margin=4,
                                   left_margin=6, right_margin=6)
            self.tv.get_buffer().set_text(texto)
            self.w = Gtk.ScrolledWindow(hexpand=True, min_content_height=90 if t == "lista" else 140)
            self.w.set_shadow_type(Gtk.ShadowType.IN)
            self.w.add(self.tv)
        else:  # info
            self.w = Gtk.Label(label=str(campo.valor), xalign=0, selectable=True, wrap=True, hexpand=True)
        self.inicial = self.valor()

    def elegir(self, _b, ventana):
        fc = Gtk.FileChooserDialog(title="Elegir carpeta", transient_for=ventana,
                                   action=Gtk.FileChooserAction.SELECT_FOLDER)
        fc.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Elegir", Gtk.ResponseType.OK)
        if self.entry.get_text() and os.path.isdir(os.path.expanduser(self.entry.get_text())):
            fc.set_current_folder(os.path.expanduser(self.entry.get_text()))
        if fc.run() == Gtk.ResponseType.OK:
            self.entry.set_text(fc.get_filename())
        fc.destroy()

    def valor(self):
        t = self.c.tipo
        if t in ("texto", "numero"):
            return self.w.get_text().strip()
        if t == "opcion":
            return self.w.get_active_id()
        if t == "carpeta":
            return self.entry.get_text().strip()
        if t == "si_no":
            return self.w.get_active()
        if t in ("lista", "pares"):
            b = self.tv.get_buffer()
            texto = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
            if t == "pares":
                return texto_a_pares(texto)
            return [l.strip() for l in texto.splitlines() if l.strip() and not l.strip().startswith("#")]
        return self.c.valor

    def cambiado(self):
        return self.c.tipo != "info" and self.valor() != self.inicial


def etiqueta_aviso(texto):
    l = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
    l.set_markup(f'<span size="small">⚠ {texto}</span>')
    l.get_style_context().add_class("aviso-config")
    return l


class DialogoConfig(Gtk.Dialog):
    """Pestañas con los formularios y archivos de un servicio. «Guardar y aplicar» llama a cfg.aplicar en un hilo."""

    def __init__(self, parent, cfg, al_terminar=None):
        super().__init__(title="Configuración · " + cfg.titulo, transient_for=parent, modal=True)
        self.cfg, self.al_terminar = cfg, al_terminar
        self.set_default_size(820, min(640, parent.get_screen().get_height() - 160))
        self.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        self.ok = self.add_button("Guardar y aplicar", Gtk.ResponseType.OK)
        self.ok.get_style_context().add_class("suggested-action")

        area = self.get_content_area()
        try:
            paginas = cfg.paginas()
        except Exception as e:
            paginas = [Formulario("Error", [Campo("", "No se ha podido leer la configuración", "info", str(e))])]
        self.editores, self.archivos = [], {}
        nb = Gtk.Notebook(scrollable=True)
        for p in paginas:
            if isinstance(p, Formulario):
                g = Gtk.Grid(column_spacing=14, row_spacing=4, margin=16)
                fila = 0
                if p.aviso:
                    g.attach(etiqueta_aviso(p.aviso), 0, fila, 2, 1)
                    fila += 1
                for c in p.campos:
                    ed = EditorCampo(c, self)
                    self.editores.append(ed)
                    lab = Gtk.Label(label=c.etiqueta, xalign=1, yalign=0.5 if c.tipo not in ("lista", "pares") else 0)
                    lab.set_margin_top(10)
                    ed.w.set_margin_top(10)
                    g.attach(lab, 0, fila, 1, 1)
                    g.attach(ed.w, 1, fila, 1, 1)
                    fila += 1
                    if c.ayuda:
                        a = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
                        a.set_markup(f'<span size="small" alpha="65%">{GLib.markup_escape_text(c.ayuda)}</span>')
                        g.attach(a, 1, fila, 1, 1)
                        fila += 1
                sc = Gtk.ScrolledWindow()
                sc.add(g)
                nb.append_page(sc, Gtk.Label(label=p.titulo))
            else:
                caja = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
                if p.ruta:
                    r = Gtk.Label(xalign=0, selectable=True)
                    r.set_markup(f'<span size="small" alpha="65%">{GLib.markup_escape_text(p.ruta)}'
                                 f'{" · solo lectura" if p.solo_lectura else ""}</span>')
                    caja.pack_start(r, False, False, 0)
                if p.aviso:
                    caja.pack_start(etiqueta_aviso(p.aviso), False, False, 0)
                tv = Gtk.TextView(monospace=True, editable=not p.solo_lectura, left_margin=8, top_margin=6)
                tv.get_buffer().set_text(p.texto)
                sc = Gtk.ScrolledWindow(vexpand=True)
                sc.set_shadow_type(Gtk.ShadowType.IN)
                sc.add(tv)
                caja.pack_start(sc, True, True, 0)
                if not p.solo_lectura:
                    self.archivos[p.titulo] = (tv.get_buffer(), p.texto)
                nb.append_page(caja, Gtk.Label(label=p.titulo))
        area.pack_start(nb, True, True, 0)

        self.estado = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=6)
        self.spinner = Gtk.Spinner()
        self.msg = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.msg.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.estado.pack_start(self.spinner, False, False, 0)
        self.estado.pack_start(self.msg, True, True, 0)
        area.pack_start(self.estado, False, False, 0)
        self.connect("response", self.on_respuesta)
        self.show_all()

    def error(self, texto):
        self.msg.set_markup(f'<span foreground="#e5484d">{GLib.markup_escape_text(texto[-1500:])}</span>')

    def on_respuesta(self, _d, resp):
        if resp != Gtk.ResponseType.OK:
            self.destroy()
            return
        cambios = {e.c.clave: e.valor() for e in self.editores if e.cambiado()}
        archivos = {}
        for nombre, (buf, original) in self.archivos.items():
            texto = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            if texto != original:
                archivos[nombre] = texto
        if not cambios and not archivos:
            self.destroy()
            return
        if cambios and archivos:
            self.error("Has cambiado el formulario y también un archivo. Guarda una cosa cada vez "
                       "(o haz el cambio solo en el archivo).")
            return
        self.ok.set_sensitive(False)
        self.spinner.start()
        self.msg.set_text("Guardando y aplicando…")

        def trabajo():
            try:
                ok, msg = self.cfg.aplicar(cambios, archivos)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            GLib.idle_add(fin, ok, msg)

        def fin(ok, msg):
            self.spinner.stop()
            self.ok.set_sensitive(True)
            if not ok:
                self.error(msg or "No se ha podido aplicar")
                return False
            parent = self.get_transient_for()
            self.destroy()
            if self.al_terminar:
                self.al_terminar()
            if msg:
                d = Gtk.MessageDialog(transient_for=parent, message_type=Gtk.MessageType.INFO,
                                      buttons=Gtk.ButtonsType.CLOSE, text="Configuración guardada")
                d.format_secondary_text(msg)
                d.run()
                d.destroy()
            return False

        import threading
        threading.Thread(target=trabajo, daemon=True).start()


# ---------- archivos de systemd (formato ini) ----------

def ini_leer(texto):
    """{(sección, clave): [valores]} con las reglas de systemd: una asignación vacía vacía la lista."""
    res, sec = {}, None
    for linea in texto.splitlines():
        l = linea.strip()
        if not l or l[0] in "#;":
            continue
        if l.startswith("[") and l.endswith("]"):
            sec = l[1:-1]
            continue
        if "=" in l and sec:
            k, v = (x.strip() for x in l.split("=", 1))
            lst = res.setdefault((sec, k), [])
            if v == "":
                lst.clear()
            else:
                lst.append(v[1:-1] if len(v) > 1 and v[0] == v[-1] == '"' and " " not in v[1:-1] else v)
    return res


def ini_poner(texto, seccion, clave, valores):
    """Sustituye todas las líneas «clave=» de la sección por «clave=valor» (una por valor); el resto no se toca."""
    lineas = texto.splitlines()
    nuevas = [f"{clave}={v}" for v in valores]
    sec, ini_sec, fin_sec, pos = None, None, None, None
    res = []
    for linea in lineas:
        l = linea.strip()
        if l.startswith("[") and l.endswith("]"):
            if sec == seccion:
                fin_sec = len(res)
            sec = l[1:-1]
            if sec == seccion:
                ini_sec = len(res) + 1
        elif sec == seccion and re.match(rf"{re.escape(clave)}\s*=", l):
            if pos is None:
                pos = len(res)
            continue
        res.append(linea)
    if sec == seccion and fin_sec is None:
        fin_sec = len(res)
    if ini_sec is None:  # la sección no existe
        return "\n".join(res + ["", f"[{seccion}]"] + nuevas) + "\n"
    if pos is None:
        pos = fin_sec
        while pos > ini_sec and not res[pos - 1].strip():  # antes de las líneas en blanco del final
            pos -= 1
    return "\n".join(res[:pos] + nuevas + res[pos:]) + "\n"


def env_partes(valores):
    """Environment= de systemd → {CLAVE: valor} (cada línea puede llevar varias asignaciones entre comillas)."""
    d = {}
    for v in valores:
        try:
            partes = shlex.split(v)
        except ValueError:
            partes = [v]
        for p in partes:
            k, _, val = p.partition("=")
            d[k] = val
    return d


def env_lineas(d):
    return [shlex.quote(f"{k}={v}") if (" " in str(v) or '"' in str(v)) else f"{k}={v}" for k, v in d.items()]


ESPACIO_SYSTEMD = ("Se acepta el formato de tiempo de systemd: 30s, 5min, 1h, 2h 30min, 1d…")


class ConfigSystemd:
    """Unidades de usuario de systemd. campos: [(Campo, unidad, sección, clave, a_valor, a_lineas)].
    a_valor(lista de valores en el archivo) → valor del formulario; a_lineas(valor, lista actual) → lista nueva."""

    def __init__(self, titulo, unidades, definir_campos, aviso=""):
        self.titulo, self.unidades, self.definir_campos, self.aviso = titulo, unidades, definir_campos, aviso

    def ruta(self, unidad):
        _, out = run(["systemctl", "--user", "show", "-P", "FragmentPath", unidad])
        return Path(out.strip() or HOME / ".config/systemd/user" / unidad)

    def paginas(self):
        self.textos = {u: self.ruta(u).read_text() for u in self.unidades}
        vals = {u: ini_leer(t) for u, t in self.textos.items()}
        self.campos = self.definir_campos(vals)
        pags = [Formulario("Ajustes", [c[0] for c in self.campos], self.aviso)]
        for u in self.unidades:
            pags.append(Archivo(u, self.textos[u], corto(self.ruta(u)),
                                aviso="Cualquier opción de systemd vale aquí (man systemd.service / systemd.timer)."))
        return pags

    def aplicar(self, cambios, archivos):
        nuevos = dict(self.textos)
        vals = {u: ini_leer(t) for u, t in self.textos.items()}
        for campo, unidad, sec, clave, _a_valor, a_lineas in self.campos:
            if campo.clave in cambios:
                try:
                    lineas = a_lineas(cambios[campo.clave], vals[unidad].get((sec, clave), []))
                except ValueError as e:
                    return False, f"{campo.etiqueta}: {e}"
                nuevos[unidad] = ini_poner(nuevos[unidad], sec, clave, lineas)
                vals[unidad] = ini_leer(nuevos[unidad])
        nuevos.update(archivos)
        cambiados = [u for u in self.unidades if nuevos[u] != self.textos[u]]
        # validar cada unidad en una carpeta temporal antes de tocar las de verdad
        # verify casi siempre termina bien y solo avisa («…, ignoring»): cuenta como error cualquier aviso que
        # no tuviera ya el archivo antes del cambio
        with tempfile.TemporaryDirectory() as d:
            for u in self.unidades:
                (Path(d) / u).write_text(self.textos[u])
            antes = {u: set(run(["systemd-analyze", "--user", "verify", str(Path(d) / u)])[1].splitlines())
                     for u in cambiados}
            for u in self.unidades:
                (Path(d) / u).write_text(nuevos[u])
            for u in cambiados:
                ok, out = run(["systemd-analyze", "--user", "verify", str(Path(d) / u)])
                nuevos_avisos = [l.replace(d + "/", "") for l in out.splitlines() if l not in antes[u]]
                if not ok or nuevos_avisos:
                    return False, f"{u} no es válido, no se ha guardado nada:\n" + "\n".join(nuevos_avisos or [out])
        for u in cambiados:
            guardar_con_copia(self.ruta(u), nuevos[u])
        run(["systemctl", "--user", "daemon-reload"])
        reiniciados = []
        for u in cambiados:
            # el .service de un timer (oneshot) se usa en la siguiente ejecución; lo demás se reinicia si está en marcha
            if run(["systemctl", "--user", "is-active", "--quiet", u])[0]:
                ok, out = run(["systemctl", "--user", "restart", u], timeout=120)
                if not ok:
                    return False, f"Guardado, pero {u} no ha vuelto a arrancar:\n{out}"
                reiniciados.append(u)
        return True, ("Reiniciado para aplicar los cambios: " + ", ".join(reiniciados)) if reiniciados else ""


def _uno(vals, sec, clave, defecto=""):
    v = vals.get((sec, clave), [])
    return v[-1] if v else defecto


def config_gateway(unidad="openclaw-gateway.service"):
    def campos(vals):
        v = vals[unidad]
        exec_ = _uno(v, "Service", "ExecStart")
        mem = re.search(r"--max-old-space-size=(\d+)", exec_)
        puerto = re.search(r"--port\s+(\d+)", exec_)

        def poner_mem(valor, actual):
            if not str(valor).isdigit():
                raise ValueError("tiene que ser un número de MB")
            linea = actual[-1] if actual else ""
            if "--max-old-space-size=" in linea:
                linea = re.sub(r"--max-old-space-size=\d+", f"--max-old-space-size={valor}", linea)
            else:
                linea = re.sub(r"(\S*node)\s", rf"\1 --max-old-space-size={valor} ", linea, count=1)
            return [linea]

        def poner_puerto(valor, actual):
            if not str(valor).isdigit():
                raise ValueError("tiene que ser un número")
            return [re.sub(r"--port\s+\d+", f"--port {valor}", actual[-1])]

        return [
            (Campo("restart", "Si se cae", "opcion", _uno(v, "Service", "Restart", "no"),
                   "Qué hace systemd si el gateway termina",
                   [("always", "Volver a arrancarlo siempre"), ("on-failure", "Volver a arrancarlo solo si falla"),
                    ("no", "Dejarlo parado")]),
             unidad, "Service", "Restart", None, lambda x, a: [x]),
            (Campo("restartsec", "Esperar antes de rearrancar", "texto", _uno(v, "Service", "RestartSec", "100ms"),
                   ESPACIO_SYSTEMD), unidad, "Service", "RestartSec", None, lambda x, a: [x]),
            (Campo("mem", "Memoria máxima de Node (MB)", "numero", mem.group(1) if mem else "",
                   "--max-old-space-size: cuánta memoria puede usar el gateway antes de que Node lo corte"),
             unidad, "Service", "ExecStart", None, poner_mem),
            (Campo("puerto", "Puerto", "numero", puerto.group(1) if puerto else "",
                   "Si lo cambias, cambia también gateway.port en ~/.openclaw/openclaw.json y el panel web de "
                   "OpenClaw. El panel de servicios lo buscará en el puerto nuevo al reiniciarse."),
             unidad, "Service", "ExecStart", None, poner_puerto),
            (Campo("env", "Variables de entorno", "pares", env_partes(v.get(("Service", "Environment"), [])),
                   "Una por línea: CLAVE=valor"),
             unidad, "Service", "Environment", None, lambda x, a: env_lineas(x)),
        ]

    class ConfigGateway(ConfigSystemd):
        def aplicar(self, cambios, archivos):
            if "puerto" in cambios:  # la variable OPENCLAW_GATEWAY_PORT tiene que ir con el --port
                env = cambios.get("env") or next(c[0].valor for c in self.campos if c[0].clave == "env")
                if "OPENCLAW_GATEWAY_PORT" in env:
                    cambios["env"] = {**env, "OPENCLAW_GATEWAY_PORT": cambios["puerto"]}
            return super().aplicar(cambios, archivos)

    return ConfigGateway("Gateway de OpenClaw", [unidad], campos,
                         aviso="Este archivo lo genera OpenClaw: si reinstalas el servicio del gateway "
                               "(openclaw gateway install) puede volver a como estaba.")


def config_limpieza(timer="openclaw-limpiar-registro.timer", servicio="openclaw-limpiar-registro.service"):
    def campos(vals):
        t, s = vals[timer], vals[servicio]
        return [
            (Campo("cada", "Cada cuánto", "texto", _uno(t, "Timer", "OnUnitActiveSec"), ESPACIO_SYSTEMD),
             timer, "Timer", "OnUnitActiveSec", None, lambda x, a: [x] if x else []),
            (Campo("arranque", "Primera vez tras encender", "texto", _uno(t, "Timer", "OnBootSec"), ESPACIO_SYSTEMD),
             timer, "Timer", "OnBootSec", None, lambda x, a: [x] if x else []),
            (Campo("persistente", "Recuperar si estaba apagado", "si_no",
                   _uno(t, "Timer", "Persistent", "false") in ("true", "yes", "1"),
                   "Si el ordenador estaba apagado cuando tocaba, lo hace nada más encender"),
             timer, "Timer", "Persistent", None, lambda x, a: ["true" if x else "false"]),
            (Campo("comando", "Comando que ejecuta", "texto", _uno(s, "Service", "ExecStart"),
                   "%h es tu carpeta personal"),
             servicio, "Service", "ExecStart", None, lambda x, a: [x]),
        ]

    return ConfigSystemd("Limpieza del registro de llamadas", [timer, servicio], campos)


# ---------- Docker (servicio del sistema): /etc/docker/daemon.json ----------

DAEMON_JSON = Path("/etc/docker/daemon.json")


class ConfigDocker:
    titulo = "Docker (servicio)"

    def paginas(self):
        try:
            self.texto = DAEMON_JSON.read_text()
        except FileNotFoundError:
            self.texto = "{}\n"
        try:
            d = json.loads(self.texto or "{}")
        except ValueError:
            d = {}
        _, raiz = run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=10)
        lo = d.get("log-opts", {})
        campos = [
            Campo("data-root", "Carpeta de datos", "carpeta", d.get("data-root", ""),
                  f"Dónde guarda Docker imágenes, contenedores y volúmenes (ahora: {raiz or '/var/lib/docker'}). "
                  "Vacío = la de siempre (/var/lib/docker). OJO: al cambiarla no se mueve nada; lo de la carpeta "
                  "vieja deja de verse hasta que vuelvas a ponerla o lo copies (sudo rsync -aP vieja/ nueva/)."),
            Campo("log-max-size", "Tamaño máximo de cada log", "texto", lo.get("max-size", ""),
                  "De los logs de los contenedores, p. ej. 10m. Vacío = sin límite. Solo afecta a contenedores "
                  "creados después."),
            Campo("log-max-file", "Logs que guardar por contenedor", "numero", lo.get("max-file", ""),
                  "Cuántos archivos rotados se quedan (con tamaño máximo), p. ej. 3"),
            Campo("dns", "Servidores DNS", "lista", d.get("dns", []),
                  "Para los contenedores, uno por línea (p. ej. 1.1.1.1). Vacío = los del ordenador."),
            Campo("registry-mirrors", "Espejos de Docker Hub", "lista", d.get("registry-mirrors", []),
                  "URLs, una por línea"),
            Campo("live-restore", "Mantener contenedores al reiniciar Docker", "si_no", d.get("live-restore", False),
                  "Los contenedores siguen corriendo mientras el servicio Docker se reinicia o actualiza"),
        ]
        aviso = "Guardar pide tu contraseña y reinicia Docker: los contenedores en marcha se paran."
        return [Formulario("Ajustes", campos, aviso),
                Archivo("daemon.json", self.texto, str(DAEMON_JSON),
                        aviso="Todas las opciones: https://docs.docker.com/reference/cli/dockerd/#daemon-configuration-file")]

    def aplicar(self, cambios, archivos):
        if "daemon.json" in archivos:
            texto = archivos["daemon.json"]
            try:
                json.loads(texto)
            except ValueError as e:
                return False, f"daemon.json no es JSON válido: {e}"
        else:
            d = json.loads(self.texto or "{}")
            for clave in ("data-root", "dns", "registry-mirrors"):
                if clave in cambios:
                    if cambios[clave]:
                        d[clave] = cambios[clave]
                    else:
                        d.pop(clave, None)
            if "live-restore" in cambios:
                d["live-restore"] = cambios["live-restore"]
            for c, k in (("log-max-size", "max-size"), ("log-max-file", "max-file")):
                if c in cambios:
                    lo = d.setdefault("log-opts", {})
                    if cambios[c]:
                        lo[k] = str(cambios[c])
                    else:
                        lo.pop(k, None)
                    if not lo:
                        d.pop("log-opts")
            if d.get("data-root") and not os.path.isabs(d["data-root"]):
                return False, "La carpeta de datos tiene que ser una ruta completa (/media/…)"
            texto = json.dumps(d, indent=2, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(texto)
        try:
            ok, out = run(["dockerd", "--validate", "--config-file", f.name])
            if not ok:
                return False, f"Docker no acepta esa configuración, no se ha guardado nada:\n{out}"
            os.chmod(f.name, 0o644)
            # un solo pkexec: copia de seguridad, instalar el archivo y reiniciar
            script = (f"[ -f {DAEMON_JSON} ] && cp {DAEMON_JSON} {DAEMON_JSON}.bak-panel; "
                      f"install -m 644 {shlex.quote(f.name)} {DAEMON_JSON} && systemctl restart docker")
            ok, out = run(["pkexec", "sh", "-c", script], timeout=180)
        finally:
            os.unlink(f.name)
        if not ok:
            return False, out or "Cancelado"
        return True, "Docker se ha reiniciado con la configuración nueva."


# ---------- contenedores ----------

def _sin_comentario(linea):
    """(texto, comentario) de una línea YAML, sin romper # dentro de comillas."""
    dentro = None
    for i, ch in enumerate(linea):
        if ch in "\"'" and (dentro is None or dentro == ch):
            dentro = None if dentro else ch
        elif ch == "#" and dentro is None and (i == 0 or linea[i - 1] in " \t"):
            return linea[:i].rstrip(), linea[i:]
    return linea.rstrip(), ""


def _escalar(v):
    """Un valor como escalar YAML, entre comillas si hace falta (y siempre con «:», p. ej. puertos 8000:8069)."""
    s = str(v)
    try:
        plano = s and yaml.safe_load(s) == s and ":" not in s and " #" not in s and not s.startswith(("*", "&", "!", "{", "["))
    except yaml.YAMLError:
        plano = False
    return s if plano else json.dumps(s, ensure_ascii=False)


def _indent(linea):
    return len(linea) - len(linea.lstrip(" "))


def compose_editar(texto, servicio, clave, valor, estilo_lista_env=False):
    """Cambia services.<servicio>.<clave> en el texto del compose tocando solo esas líneas.
    Conserva los comentarios del resto del archivo y los de los elementos que no cambian.
    valor: str (escalar) · list · dict · None (quitar la clave)."""
    lineas = texto.split("\n")
    # bloque de services:
    i_serv = next((i for i, l in enumerate(lineas) if re.match(r"services:\s*(#.*)?$", l)), None)
    if i_serv is None:
        raise ValueError("no encuentro «services:» en el compose")
    i_svc = None
    for i in range(i_serv + 1, len(lineas)):
        l = lineas[i]
        if l.strip() and not l.lstrip().startswith("#") and _indent(l) == 0:
            break
        if re.match(rf"\s+{re.escape(servicio)}:\s*(#.*)?$", l):
            i_svc = i
            break
    if i_svc is None:
        raise ValueError(f"no encuentro el servicio {servicio} en el compose")
    n_svc = _indent(lineas[i_svc])
    fin_svc, n_clave = len(lineas), None
    for i in range(i_svc + 1, len(lineas)):
        l = lineas[i]
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        if _indent(l) <= n_svc:
            fin_svc = i
            break
        if n_clave is None:
            n_clave = _indent(l)
    n_clave = n_clave or n_svc + 2
    pre = " " * n_clave
    # la clave dentro del servicio
    i_k = next((i for i in range(i_svc + 1, fin_svc) if re.match(rf"{pre}{re.escape(clave)}:(\s|$)", lineas[i])), None)
    fin_k = None
    viejos = {}  # elemento → (comentarios de antes, comentario de la línea, línea original)
    if i_k is not None:
        fin_k = fin_svc
        pendientes = []
        for i in range(i_k + 1, fin_svc):
            l = lineas[i]
            if l.strip() and not l.lstrip().startswith("#") and _indent(l) <= n_clave:
                fin_k = i
                break
            if not l.strip():
                continue
            if l.lstrip().startswith("#"):
                pendientes.append(l)
                continue
            cuerpo, com = _sin_comentario(l)
            item = cuerpo.strip()
            item = item[2:].strip() if item.startswith("- ") else item
            try:
                parsed = yaml.safe_load(item)
            except yaml.YAMLError:
                parsed = item
            if isinstance(parsed, dict) and len(parsed) == 1:
                k = str(next(iter(parsed)))
            else:
                k = str(parsed)
                if "=" in k and estilo_lista_env:
                    k = k.split("=", 1)[0]
            viejos[k] = (pendientes, com, l)
            pendientes = []
        while fin_k > i_k + 1 and (not lineas[fin_k - 1].strip() or
                                   (lineas[fin_k - 1].lstrip().startswith("#") and _indent(lineas[fin_k - 1]) <= n_clave)):
            fin_k -= 1  # los comentarios y huecos del final son de lo que viene después
    com_clave = _sin_comentario(lineas[i_k])[1] if i_k is not None else ""

    nuevas = []
    sub = pre + "  "
    if valor is None or valor == [] or valor == {} or valor == "":
        nuevas = []
    elif isinstance(valor, (list, dict)):
        nuevas.append(f"{pre}{clave}:" + (f"  {com_clave}" if com_clave else ""))
        items = valor.items() if isinstance(valor, dict) else [(str(x), None) for x in valor]
        for k, v in items:
            if isinstance(valor, dict):
                k = str(k)
                linea = f"{sub}- {_escalar(f'{k}={v}')}" if estilo_lista_env else f"{sub}{k}: {_escalar(v)}"
            else:
                linea = f"{sub}- {_escalar(k)}"
            if k in viejos:
                coms, com, original = viejos[k]
                nuevas += coms
                try:
                    igual = yaml.safe_load(_sin_comentario(original)[0].strip().lstrip("- ")) == \
                        yaml.safe_load(linea.strip().lstrip("- "))
                except yaml.YAMLError:
                    igual = False
                nuevas.append(original if igual else linea + (f"  {com}" if com else ""))
            else:
                nuevas.append(linea)
    else:
        nuevas.append(f"{pre}{clave}: {_escalar(valor)}" + (f"  {com_clave}" if com_clave else ""))

    if i_k is not None:
        lineas[i_k:fin_k] = nuevas
    else:
        pos = fin_svc
        while pos > i_svc + 1 and not lineas[pos - 1].strip():
            pos -= 1
        lineas[pos:pos] = nuevas
    return "\n".join(lineas)


REINICIO = [("no", "No (solo cuando lo arranques tú)"), ("always", "Siempre, también al encender"),
            ("unless-stopped", "Siempre, salvo si lo paraste tú"), ("on-failure", "Solo si falla")]


class ConfigContenedor:
    def __init__(self, nombre):
        self.nombre = nombre
        self.titulo = "Contenedor " + nombre

    def _inspect(self):
        ok, out = run(["docker", "inspect", self.nombre], timeout=15)
        if not ok:
            raise RuntimeError(out)
        return json.loads(out)[0]

    def paginas(self):
        ins = self._inspect()
        lab = ins["Config"].get("Labels") or {}
        self.compose = lab.get("com.docker.compose.project.config_files", "").split(",")[0] or None
        self.dir = lab.get("com.docker.compose.project.working_dir")
        self.svc = lab.get("com.docker.compose.service")
        self.corriendo = ins["State"]["Running"]
        if self.compose and os.path.isfile(self.compose):
            return self._paginas_compose()
        return self._paginas_sueltas(ins)

    # contenedor de docker compose: se edita su docker-compose.yml
    def _paginas_compose(self):
        self.texto = Path(self.compose).read_text()
        doc = yaml.safe_load(self.texto) or {}
        s = (doc.get("services") or {}).get(self.svc, {})
        env = s.get("environment") or {}
        self.env_lista = isinstance(env, list)
        if self.env_lista:
            env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
        self.inicial = {"image": s.get("image", ""), "restart": str(s.get("restart", "no")),
                        "ports": [str(p) for p in s.get("ports") or []],
                        "volumes": [str(v) for v in s.get("volumes") or []],
                        "environment": {str(k): "" if v is None else str(v) for k, v in env.items()},
                        "mem_limit": str(s.get("mem_limit", "")), "cpus": str(s.get("cpus", ""))}
        i = self.inicial
        campos = [
            Campo("info", "Proyecto", "info", f"{corto(self.compose)} · servicio «{self.svc}»"),
            Campo("image", "Imagen", "texto", i["image"],
                  "p. ej. postgres:16-alpine. Si el servicio tiene build:, es el nombre de la imagen que se construye."),
            Campo("restart", "Arrancar solo", "opcion", i["restart"], "", REINICIO),
            Campo("ports", "Puertos", "lista", i["ports"],
                  "Uno por línea: PUERTO_DEL_PC:PUERTO_DEL_CONTENEDOR. Con 127.0.0.1: delante, solo desde este PC."),
            Campo("volumes", "Carpetas y volúmenes", "lista", i["volumes"],
                  "Uno por línea: ORIGEN:RUTA_EN_EL_CONTENEDOR[:ro]. ORIGEN puede ser una carpeta (./algo, /media/…) "
                  "o un volumen con nombre."),
            Campo("environment", "Variables de entorno", "pares", i["environment"],
                  "Una por línea: CLAVE=valor. ${VAR} se toma del archivo .env del proyecto."),
            Campo("mem_limit", "Memoria máxima", "texto", i["mem_limit"], "p. ej. 2g o 512m. Vacío = sin límite."),
            Campo("cpus", "CPUs máximas", "texto", i["cpus"], "p. ej. 1.5. Vacío = sin límite."),
        ]
        aviso = ("Se guarda en el docker-compose.yml (que también usan los demás contenedores del proyecto). "
                 + ("Al guardar se recrea el contenedor para aplicarlo; los datos de los volúmenes se conservan."
                    if self.corriendo else "Se aplicará la próxima vez que lo arranques."))
        pags = [Formulario("Ajustes", campos, aviso),
                Archivo("docker-compose.yml", self.texto, self.compose,
                        aviso="El archivo entero: afecta a todos los servicios del proyecto.")]
        env_path = Path(self.dir or os.path.dirname(self.compose)) / ".env"
        if env_path.exists():
            pags.append(Archivo(".env", env_path.read_text(), str(env_path),
                                aviso="Contraseñas y valores que usa el compose como ${VARIABLE}."))
        return pags

    def _validar_compose(self, texto_yml, texto_env=None):
        """docker compose config con los archivos nuevos, sin tocar los de verdad."""
        carpeta = os.path.dirname(self.compose)
        tmp = Path(carpeta) / ".docker-compose.panel-tmp.yml"
        tmp.write_text(texto_yml)
        cmd = ["docker", "compose", "-f", str(tmp), "--project-directory", self.dir or carpeta]
        envtmp = None
        if texto_env is not None:
            envtmp = Path(carpeta) / ".env.panel-tmp"
            envtmp.write_text(texto_env)
            cmd += ["--env-file", str(envtmp)]
        try:
            return run(cmd + ["config", "-q"], timeout=30)
        finally:
            tmp.unlink(missing_ok=True)
            if envtmp:
                envtmp.unlink(missing_ok=True)

    def _aplicar_compose(self, cambios, archivos):
        texto = archivos.get("docker-compose.yml", self.texto)
        if cambios:
            for clave in ("image", "restart", "ports", "volumes", "environment", "mem_limit", "cpus"):
                if clave in cambios:
                    texto = compose_editar(texto, self.svc, clave, cambios[clave],
                                           estilo_lista_env=clave == "environment" and self.env_lista)
        env_nuevo = archivos.get(".env")
        ok, out = self._validar_compose(texto, env_nuevo)
        if not ok:
            return False, f"Docker Compose no acepta el cambio, no se ha guardado nada:\n{out}"
        if texto != self.texto:
            guardar_con_copia(self.compose, texto)
        if env_nuevo is not None:
            guardar_con_copia(Path(self.dir or os.path.dirname(self.compose)) / ".env", env_nuevo)
        if not self.corriendo:
            return True, "Guardado. Se aplicará cuando arranques el contenedor."
        ok, out = run(["docker", "compose", "-f", self.compose, "--project-directory",
                       self.dir or os.path.dirname(self.compose), "up", "-d", "--no-deps", self.svc], timeout=300)
        if not ok:
            return False, f"Guardado, pero no se ha podido recrear el contenedor:\n{out}"
        return True, "Contenedor recreado con la configuración nueva."

    # contenedor suelto (sin compose): solo lo que docker update puede cambiar en caliente
    def _paginas_sueltas(self, ins):
        hc = ins["HostConfig"]
        mem = hc.get("Memory") or 0
        cpus = (hc.get("NanoCpus") or 0) / 1e9
        self.inicial = {"restart": hc["RestartPolicy"]["Name"] or "no"}
        puertos = [f"{v[0].get('HostIp') + ':' if v and v[0].get('HostIp') else ''}{v[0]['HostPort']}:{k}"
                   for k, v in (hc.get("PortBindings") or {}).items() if v]
        campos = [
            Campo("restart", "Arrancar solo", "opcion", self.inicial["restart"], "", REINICIO),
            Campo("mem", "Memoria máxima", "texto", f"{mem // 2**20}m" if mem else "", "p. ej. 2g. Vacío = sin límite"),
            Campo("cpus", "CPUs máximas", "texto", f"{cpus:g}" if cpus else "", "p. ej. 1.5. Vacío = sin límite"),
            Campo("i1", "Imagen", "info", ins["Config"]["Image"]),
            Campo("i2", "Puertos", "info", "\n".join(puertos) or "—"),
            Campo("i3", "Montajes", "info", "\n".join(f"{m.get('Source') or m.get('Name')} → {m['Destination']}"
                                                      for m in ins.get("Mounts", [])) or "—"),
        ]
        return [Formulario("Ajustes", campos, "No es de docker compose: la imagen, los puertos y los montajes solo se "
                                              "pueden cambiar creándolo de nuevo con docker run.")]

    def aplicar(self, cambios, archivos):
        if self.compose and os.path.isfile(self.compose):
            return self._aplicar_compose(cambios, archivos)
        cmd = ["docker", "update"]
        if "restart" in cambios:
            cmd += ["--restart", cambios["restart"]]
        if "mem" in cambios:
            cmd += ["--memory", cambios["mem"] or "0", "--memory-swap", "-1"]
        if "cpus" in cambios:
            cmd += ["--cpus", cambios["cpus"] or "0"]
        ok, out = run(cmd + [self.nombre])
        return ok, "" if ok else out


# ---------- servicios propios del panel ----------

class ConfigAjustes:
    """Ajustes guardados en ajustes.json. claves: cuáles de AJUSTES_DEF se muestran."""

    DEF = {
        "creador_dir": ("Carpeta del Creador", "carpeta", "Donde está app.py"),
        "creador_puerto": ("Puerto del Creador", "numero",
                           "Se le pasa al arrancarlo (CREADOR_PORT). Si está en marcha, se reinicia."),
        "ollama_url": ("Dirección de Ollama", "texto", "p. ej. http://127.0.0.1:11434. Solo la usa este panel; "
                                                         "OpenClaw y el Creador tienen la suya."),
        "refresco_s": ("Comprobar cada (segundos)", "numero", ""),
    }

    def __init__(self, titulo, claves, al_guardar):
        self.titulo, self.claves, self.al_guardar = titulo, claves, al_guardar

    def paginas(self):
        a = leer_ajustes()
        return [Formulario("Ajustes", [Campo(k, self.DEF[k][0], self.DEF[k][1], a[k], self.DEF[k][2])
                                       for k in self.claves])]

    def aplicar(self, cambios, archivos):
        a = leer_ajustes()
        for k, v in cambios.items():
            if self.DEF[k][1] == "numero":
                if not str(v).isdigit() or int(v) < 1:
                    return False, f"{self.DEF[k][0]}: tiene que ser un número"
                v = int(v)
            if self.DEF[k][1] == "carpeta" and not os.path.isdir(os.path.expanduser(v)):
                return False, f"{self.DEF[k][0]}: la carpeta no existe"
            a[k] = v
        guardar_ajustes(a)
        return self.al_guardar(cambios)


def corto(ruta):
    return str(ruta).replace(str(HOME), "~", 1)
