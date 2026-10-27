# Panel de servicios

Ver qué está en marcha en el ordenador y arrancarlo o pararlo: servicios de systemd, Docker y sus contenedores,
tus propios programas (Python, Node…) y cualquier servidor de desarrollo que esté escuchando en un puerto.
Hecho para Linux Mint (Cinnamon), aunque funciona en cualquier Linux con systemd.

Tiene dos versiones que comparten el mismo código:

| | Archivo | Cómo se abre |
|---|---|---|
| App de escritorio (GTK3) | `panel.py` (+ `configuracion.py`) | Menú → «Panel de servicios» |
| Terminal (también por SSH) | `terminal.py` | `servicios` |

La de terminal solo enseña lo que existe en cada equipo: si no hay OpenClaw u Ollama, esas secciones no salen.

## Instalar

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 python3-yaml python3-venv   # si faltan
git clone https://github.com/danii1199/panel-servicios.git
cd panel-servicios
./instalar.sh                  # terminal + escritorio
./instalar.sh --solo-terminal  # solo el comando «servicios»
```

`instalar.sh` crea un entorno `.venv` con [Textual](https://textual.textualize.io/) y el comando `~/.local/bin/servicios`.

## Teclas de la versión de terminal

| Tecla | Qué hace |
|---|---|
| ↑ ↓ o ratón | Moverse |
| Enter / espacio | Arrancar o parar |
| `l` | Log en directo (`/` filtrar, `f` seguir el final, `Esc` volver) |
| `e` | Arrancar al encender (sí/no) |
| `g` | Liberar la GPU de Ollama |
| `r` | Comprobar ahora |
| `q` | Salir |

Para el servicio Docker la interfaz se aparta un momento y `sudo` pide la contraseña en la misma terminal.

## Dónde guarda las cosas

- `~/.config/panel-servicios/servicios.json`: tus servicios (nombre, carpeta, comando y puerto). Se añaden desde la
  app de escritorio o editando el archivo.
- `~/.config/panel-servicios/ajustes.json`: ajustes del panel (dirección de Ollama, cada cuánto comprobar…).
- `~/.local/state/panel-servicios/logs/`: la salida de los servicios que arranca el panel.
