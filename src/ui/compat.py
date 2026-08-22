import os
import gi
import sys
from pathlib import Path

# Add local build AppDir to sys.path if romm_sync_engine is present there
_local_engine = Path(__file__).parent.parent.parent / 'build' / 'AppDir' / 'usr' / 'bin'
if _local_engine.exists() and str(_local_engine) not in sys.path:
    sys.path.insert(0, str(_local_engine))

try:
    gi.require_version('Gtk', '4.0')
except ValueError:
    pass

try:
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Gdk, Adw, GLib, Gio, GObject
    HAS_ADW = True
    try:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
    except Exception:
        pass
except ValueError:
    from gi.repository import Gtk, Gdk, GLib, Gio, GObject
    HAS_ADW = False

class MockAdw:
    class Application(Gtk.Application):
        pass

    class ApplicationWindow(Gtk.ApplicationWindow):
        pass

    class Window(Gtk.Window):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

        def set_content(self, child):
            self.set_child(child)

    class PreferencesWindow(Gtk.Window):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.set_modal(True)
            self.set_default_size(800, 600)
            self._main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            self._main_box.set_margin_top(12)
            self._main_box.set_margin_bottom(12)
            self._main_box.set_margin_start(12)
            self._main_box.set_margin_end(12)
            self.set_child(self._main_box)

        def add(self, page):
            self._main_box.append(page)

        def set_content_width(self, width):
            self.set_default_size(width, -1)

        def set_content_height(self, height):
            self.set_default_size(-1, height)

    class PreferencesDialog(PreferencesWindow):
        pass

    class PreferencesPage(Gtk.Box):
        def __init__(self, **kwargs):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
            self.set_margin_top(12)
            self.set_margin_bottom(12)
            self.set_margin_start(12)
            self.set_margin_end(12)
            self.set_spacing(12)

        def add(self, child):
            self.append(child)

    class PreferencesGroup(Gtk.Box):
        def __init__(self, **kwargs):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
            self.set_spacing(4)
            self._title_label = None

        def set_title(self, title):
            if self._title_label is None:
                self._title_label = Gtk.Label()
                self._title_label.set_halign(Gtk.Align.START)
                self._title_label.add_css_class("heading")
                self._title_label.set_margin_bottom(6)
                self.prepend(self._title_label)
            self._title_label.set_text(title)

        def add(self, child):
            self.append(child)

    class HeaderBar(Gtk.HeaderBar):
        pass

    class ToolbarView(Gtk.Box):
        def __init__(self, **kwargs):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
            self._top_bars = []
            self._content = None

        def add_top_bar(self, widget):
            self._top_bars.append(widget)
            self.prepend(widget)

        def set_content(self, widget):
            if self._content:
                self.remove(self._content)
            self._content = widget
            if widget:
                self.append(widget)

    class _RowBase(Gtk.Box):
        """Standard base class for mock Adw preference rows with prefix, title, and suffix containers."""
        def __init__(self, **kwargs):
            super().__init__(orientation=Gtk.Orientation.HORIZONTAL, **kwargs)
            self.set_spacing(12)
            self.set_margin_top(6)
            self.set_margin_bottom(6)
            self.set_margin_start(12)
            self.set_margin_end(12)

            self._prefix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.append(self._prefix_box)

            self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self._title_box.set_hexpand(True)
            self.append(self._title_box)

            self._title_label = None
            self._subtitle_label = None

            self._suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.append(self._suffix_box)

        def set_title(self, title):
            if self._title_label is None:
                self._title_label = Gtk.Label()
                self._title_label.set_halign(Gtk.Align.START)
                self._title_box.append(self._title_label)
            self._title_label.set_text(title)

        def set_subtitle(self, subtitle):
            if self._subtitle_label is None:
                self._subtitle_label = Gtk.Label()
                self._subtitle_label.set_halign(Gtk.Align.START)
                self._subtitle_label.add_css_class("dim-label")
                self._title_box.append(self._subtitle_label)
            self._subtitle_label.set_text(subtitle)

        def get_subtitle(self):
            return self._subtitle_label.get_text() if self._subtitle_label else ""

        def set_subtitle_lines(self, lines):
            if self._subtitle_label:
                self._subtitle_label.set_lines(lines)
                self._subtitle_label.set_wrap(True)

        def add_prefix(self, widget):
            self._prefix_box.append(widget)

        def add_suffix(self, widget):
            self._suffix_box.append(widget)

    class ActionRow(_RowBase):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._child = None

        def set_child(self, child):
            if self._child:
                self._suffix_box.remove(self._child)
            self._child = child
            if child:
                self._suffix_box.append(child)

        def set_activatable(self, activatable):
            pass

        def set_activatable_widget(self, widget):
            pass

    class SwitchRow(_RowBase):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.switch = Gtk.Switch()
            self.switch.set_valign(Gtk.Align.CENTER)
            self.add_suffix(self.switch)

        def get_active(self):
            return self.switch.get_active()

        def set_active(self, active):
            self.switch.set_active(active)

        def set_sensitive(self, sensitive):
            self.switch.set_sensitive(sensitive)

        def connect(self, signal_name, callback):
            if signal_name == 'notify::active':
                return self.switch.connect('notify::active', callback)
            return super().connect(signal_name, callback)

    class EntryRow(_RowBase):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.entry = Gtk.Entry()
            self.entry.set_hexpand(True)
            self.entry.set_valign(Gtk.Align.CENTER)
            self._title_box.append(self.entry)

        def set_title(self, title):
            super().set_title(title)
            if self._title_label:
                self._title_label.set_valign(Gtk.Align.CENTER)

        def get_text(self):
            return self.entry.get_text()

        def set_text(self, text):
            self.entry.set_text(text)

        def connect(self, signal_name, callback):
            if signal_name in ('activate', 'entry-activated'):
                return self.entry.connect('activate', callback)
            elif signal_name in ('changed', 'notify::text'):
                return self.entry.connect('changed', callback)
            return super().connect(signal_name, callback)

    class PasswordEntryRow(EntryRow):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.entry.set_visibility(False)

    class ExpanderRow(Gtk.Box):
        def __init__(self, **kwargs):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, **kwargs)
            self.set_spacing(0)

            # Header row with prefix, title, and suffix
            self._header = MockAdw._RowBase()

            # Create expander with custom header
            self.expander = Gtk.Expander()
            self.expander.set_label_widget(self._header)
            self.append(self.expander)

            # Content box
            self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.expander.set_child(self.content_box)

        def set_title(self, title):
            self._header.set_title(title)

        def set_subtitle(self, subtitle):
            self._header.set_subtitle(subtitle)

        def get_subtitle(self):
            return self._header.get_subtitle()

        def add_prefix(self, widget):
            self._header.add_prefix(widget)

        def add_suffix(self, widget):
            self._header.add_suffix(widget)

        def add_row(self, row):
            self.content_box.append(row)

        def set_expanded(self, expanded):
            self.expander.set_expanded(expanded)

        def get_expanded(self):
            return self.expander.get_expanded()

        def set_enable_expansion(self, enable):
            self.expander.set_sensitive(enable)

        def get_enable_expansion(self):
            return self.expander.get_sensitive()

        def connect(self, signal_name, callback):
            if signal_name in ('notify::expanded', 'notify::enable-expansion'):
                return self.expander.connect(signal_name, callback)
            return super().connect(signal_name, callback)

    class SpinRow(_RowBase):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.spin = Gtk.SpinButton()
            self.spin.set_valign(Gtk.Align.CENTER)
            self.add_suffix(self.spin)

        def get_value(self):
            return self.spin.get_value()

        def set_value(self, value):
            self.spin.set_value(value)

        def set_range(self, min_val, max_val):
            self.spin.set_range(min_val, max_val)

        def set_adjustment(self, adjustment):
            self.spin.set_adjustment(adjustment)

        def connect(self, signal_name, callback):
            if signal_name in ('notify::value', 'value-changed'):
                return self.spin.connect('value-changed', callback)
            return super().connect(signal_name, callback)

    class ComboRow(_RowBase):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.combo = Gtk.DropDown()
            self.combo.set_valign(Gtk.Align.CENTER)
            self.add_suffix(self.combo)

        def set_model(self, model):
            self.combo.set_model(model)

        def get_selected(self):
            return self.combo.get_selected()

        def set_selected(self, index):
            self.combo.set_selected(index)

        def connect(self, signal_name, callback):
            if signal_name in ('notify::selected', 'notify::selected-item', 'changed'):
                return self.combo.connect('notify::selected', callback)
            return super().connect(signal_name, callback)

    class AlertDialog(Gtk.Dialog):
        def __init__(self, heading="", body="", **kwargs):
            super().__init__(**kwargs)
            self.set_modal(True)
            self._heading = heading
            self._body = body
            self._responses = {}
            self._close_response = None

            box = self.get_content_area() if hasattr(self, 'get_content_area') else self
            if heading:
                h_label = Gtk.Label(label=heading)
                h_label.add_css_class("title-2")
                h_label.set_margin_bottom(6)
                box.append(h_label)
            if body:
                b_label = Gtk.Label(label=body)
                b_label.set_wrap(True)
                b_label.set_margin_bottom(12)
                box.append(b_label)

        @classmethod
        def new(cls, heading="", body=""):
            return cls(heading=heading, body=body)

        def add_response(self, response_id, label):
            self._responses[response_id] = label
            self.add_button(label, response_id)

        def set_response_appearance(self, response_id, appearance):
            pass

        def set_close_response(self, response_id):
            self._close_response = response_id

        def choose(self, parent, cancellable, callback):
            def on_response(dialog, response):
                dialog.destroy()
                if callback:
                    callback(dialog, response)
            self.connect('response', on_response)
            self.present(parent)

    class ResponseAppearance:
        DEFAULT = 0
        SUGGESTED = 1
        DESTRUCTIVE = 2

    class AboutWindow(Gtk.AboutDialog):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

        def set_website(self, url):
            if hasattr(self, 'set_website_url'):
                self.set_website_url(url)

        def set_issue_url(self, url):
            pass

def detect_desktop_environment(manual_de=None):
    """Detect current Linux desktop environment or return manual override"""
    if manual_de:
        de_map = {
            'gnome': 'GNOME',
            'kde': 'KDE',
            'steamos': 'STEAM_OS',
            'xfce': 'XFCE',
            'cinnamon': 'CINNAMON',
            'mate': 'MATE',
            'generic': 'GENERIC'
        }
        return de_map.get(manual_de.lower(), 'GENERIC')

    if os.path.exists('/etc/os-release'):
        try:
            with open('/etc/os-release', 'r') as f:
                os_release_content = f.read().lower()
                if 'steamos' in os_release_content or 'steamdeck' in os_release_content:
                    return 'STEAM_OS'
        except Exception:
            pass

    xdg_current = os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
    desktop_session = os.environ.get('DESKTOP_SESSION', '').upper()

    if 'GNOME' in xdg_current or 'GNOME' in desktop_session:
        return 'GNOME'
    elif 'KDE' in xdg_current or 'PLASMA' in xdg_current or 'KDE' in desktop_session:
        return 'KDE'
    elif 'XFCE' in xdg_current or 'XFCE' in desktop_session:
        return 'XFCE'
    elif 'CINNAMON' in xdg_current or 'CINNAMON' in desktop_session:
        return 'CINNAMON'
    elif 'MATE' in xdg_current or 'MATE' in desktop_session:
        return 'MATE'
    else:
        return 'GENERIC'

def get_de_custom_css(de):
    """Generate dynamic CSS rules tailored for the detected desktop environment"""
    css_snippets = []
    if de == 'GNOME':
        css_snippets.append("""
            .card, expanderrow {
                border-radius: 12px;
            }
            scrolledwindow.data-table, .data-table columnview {
                border-radius: 12px;
            }
            .data-table row {
                min-height: 38px;
            }
        """)
    elif de == 'STEAM_OS':
        css_snippets.append("""
            .card, expanderrow {
                border-radius: 8px;
                border: 1px solid alpha(@borders, 0.4);
            }
            scrolledwindow.data-table, .data-table columnview {
                border-radius: 10px;
            }
            .data-table row {
                min-height: 44px;
                font-size: 1.05em;
            }
            button.column-gear-btn {
                min-width: 24px;
                min-height: 24px;
            }
            :focus {
                outline: 2px solid @accent_bg_color;
                outline-offset: 2px;
            }
        """)
    else:
        de_label = "KDE Breeze" if de == 'KDE' else f"{de} Traditional Desktop"
        border_rad = "4px" if de == 'KDE' else "2px"
        css_snippets.append(f"""
            .card {{
                background-color: @window_bg_color;
                box-shadow: none;
                border: 1px solid @borders;
                border-radius: {border_rad};
            }}
            expanderrow, preferencesgroup > list {{
                background-color: @window_bg_color;
                border: 1px solid @borders;
                border-radius: {border_rad};
                box-shadow: none;
            }}
            scrolledwindow.data-table, .data-table columnview {{
                border-radius: {border_rad};
                border: 1px solid @borders;
                background-color: @view_bg_color;
            }}
            .data-table row {{
                min-height: 32px;
                border-bottom: 1px solid alpha(@borders, 0.2);
            }}
            button {{
                border-radius: {border_rad};
            }}
            popovermenubar.traditional-top-menubar {{
                background-color: @window_bg_color;
                border-bottom: 1px solid alpha(@borders, 0.5);
                padding: 1px 4px;
                font-family: -gtk-system-font;
            }}
            popovermenubar.traditional-top-menubar item {{
                padding: 4px 8px;
                border-radius: {border_rad};
            }}
            headerbar.traditional-headerbar {{
                background-color: @window_bg_color;
                border-bottom: 1px solid alpha(@borders, 0.4);
                box-shadow: none;
            }}
        """)
    return "\n".join(css_snippets)
