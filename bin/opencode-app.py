#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import Gtk, Vte, GLib, Gdk
import os

user = os.environ["USER"]
icon_path = os.path.expanduser("~/.local/share/icons/opencode.jpeg")
workdir = f"/media/{user}/Linux_Apps/Projets_AI"
opencode_bin = "/media/marcs/Linux_Apps/.opencode/bin/opencode"

win = Gtk.Window(title="OpenCode")
win.set_wmclass("opencode", "OpenCode")
win.set_role("opencode")
win.set_default_size(900, 600)

if os.path.exists(icon_path):
    win.set_icon_from_file(icon_path)

css = b"window { background: #1e1e2e; }"
screen = Gdk.Screen.get_default()
provider = Gtk.CssProvider()
provider.load_from_data(css)
Gtk.StyleContext.add_provider_for_screen(
    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
)

terminal = Vte.Terminal()
terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
terminal.set_font_scale(1.0)
terminal.set_scrollback_lines(100000)

launch_cmd = "/media/marcs/Linux_Apps/Projets_AI/tools/start-tui.sh"
env = [
    f"PATH=/media/marcs/Linux_Apps/.local/bin:{os.environ.get('PATH', '')}",
    "PIPX_BIN_DIR=/media/marcs/Linux_Apps/.local/bin",
]

terminal.spawn_sync(
    Vte.PtyFlags.DEFAULT,
    workdir,
    ["/bin/bash", "-c", f"{launch_cmd}; exec bash"],
    env,
    GLib.SpawnFlags.DO_NOT_REAP_CHILD,
    None,
    None,
    None
)

win.add(terminal)
win.connect("delete-event", Gtk.main_quit)
win.show_all()
Gtk.main()
