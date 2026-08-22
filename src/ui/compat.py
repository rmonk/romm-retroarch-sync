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

    class PreferencesWindow(Gtk.Window):
        def __init__(self):
            super().__init__()
            self.set_modal(True)
            self.set_default_size(800, 600)

    class PreferencesPage(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self.set_margin_top(12)
            self.set_margin_bottom(12)
            self.set_margin_start(12)
            self.set_margin_end(12)
            self.set_spacing(12)

        def add(self, child):
            super().append(child)

    class PreferencesGroup(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self.set_spacing(0)
            self._title_label = None

        def set_title(self, title):
            if self._title_label is None:
                self._title_label = Gtk.Label()
                self._title_label.set_halign(Gtk.Align.START)
                self._title_label.set_margin_top(12)
                self._title_label.set_margin_bottom(6)
                self._title_label.set_margin_start(12)
                self.prepend(self._title_label)
            # Handle HTML entities in title - decode and escape for markup
            import html as html_module
            decoded_title = html_module.unescape(title)
            escaped_title = decoded_title.replace('&', '&amp;')
            try:
                self._title_label.set_markup(f"<b>{escaped_title}</b>")
            except Exception:
                # Fallback to plain text if markup fails
                self._title_label.set_text(decoded_title)

        def add(self, child):
            super().append(child)

    class HeaderBar(Gtk.HeaderBar):
        pass

    class ToolbarView(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self._header = None
            self._content = None

        def add_top_bar(self, header):
            if self._header is None:
                self._header = header
                self.prepend(header)
            else:
                # Replace existing header
                self.remove(self._header)
                self._header = header
                self.prepend(header)

        def set_content(self, content):
            if self._content is not None:
                self.remove(self._content)
            self._content = content
            self.append(content)

    class ActionRow(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
            self.set_spacing(12)
            self.set_margin_top(6)
            self.set_margin_bottom(6)
            self.set_margin_start(12)
            self.set_margin_end(12)
            self._title_box = None
            self._title_label = None
            self._subtitle_label = None

        def set_title(self, title):
            if self._title_box is None:
                self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                self._title_box.set_hexpand(True)
                self._title_label = Gtk.Label(label=title)
                self._title_label.set_halign(Gtk.Align.START)
                self._title_box.append(self._title_label)
                self.prepend(self._title_box)
            else:
                self._title_label.set_text(title)

        def set_subtitle(self, subtitle):
            if self._title_box is None:
                self.set_title("")  # Initialize title box
            if self._subtitle_label is None:
                self._subtitle_label = Gtk.Label(label=subtitle)
                self._subtitle_label.set_halign(Gtk.Align.START)
                self._subtitle_label.add_css_class("dim-label")
                self._title_box.append(self._subtitle_label)
            else:
                self._subtitle_label.set_text(subtitle)

        def add_suffix(self, widget):
            self.append(widget)

        def add_prefix(self, widget):
            if self._title_box is None:
                self.set_title("")  # Initialize title box
            self.prepend(widget)

        def set_child(self, widget):
            # Simply append the widget - ActionRow with set_child replaces content
            self.append(widget)

    class SwitchRow(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
            self.set_spacing(12)
            self.set_margin_top(6)
            self.set_margin_bottom(6)
            self.set_margin_start(12)
            self.set_margin_end(12)
            self._title_box = None
            self._title_label = None
            self._subtitle_label = None
            self.switch = Gtk.Switch()
            self.switch.set_valign(Gtk.Align.CENTER)
            self.append(self.switch)

        def set_title(self, title):
            if self._title_box is None:
                self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                self._title_box.set_hexpand(True)
                self._title_label = Gtk.Label(label=title)
                self._title_label.set_halign(Gtk.Align.START)
                self._title_box.append(self._title_label)
                self.prepend(self._title_box)
            else:
                self._title_label.set_text(title)

        def set_subtitle(self, subtitle):
            if self._title_box is None:
                self.set_title("")  # Initialize title box
            if self._subtitle_label is None:
                self._subtitle_label = Gtk.Label(label=subtitle)
                self._subtitle_label.set_halign(Gtk.Align.START)
                self._subtitle_label.add_css_class("dim-label")
                self._title_box.append(self._subtitle_label)
            else:
                self._subtitle_label.set_text(subtitle)

        def get_active(self):
            return self.switch.get_active()

        def set_active(self, active):
            self.switch.set_active(active)

        def connect(self, signal_name, callback):
            if signal_name == 'notify::active':
                return self.switch.connect('notify::active', callback)
            return super().connect(signal_name, callback)

    class EntryRow(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self.set_spacing(6)
            self.set_margin_top(6)
            self.set_margin_bottom(6)
            self.set_margin_start(12)
            self.set_margin_end(12)
            self.entry = Gtk.Entry()
            self.append(self.entry)

        def set_title(self, title):
            label = Gtk.Label(label=title)
            label.set_halign(Gtk.Align.START)
            self.prepend(label)

        def get_text(self):
            return self.entry.get_text()

        def set_text(self, text):
            self.entry.set_text(text)

        def connect(self, signal_name, callback):
            if signal_name in ('activate', 'entry-activated'):
                # Forward to the internal entry widget's 'activate' signal
                # (Adw.EntryRow uses 'entry-activated', Gtk.Entry uses 'activate')
                return self.entry.connect('activate', callback)
            else:
                return super().connect(signal_name, callback)

    class PasswordEntryRow(EntryRow):
        def __init__(self):
            super().__init__()
            self.entry.set_visibility(False)

    class ExpanderRow(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self.set_spacing(0)

            # Header box to hold title, prefix, and suffix
            self.header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            self.header_box.set_spacing(6)
            self.header_box.set_margin_top(6)
            self.header_box.set_margin_bottom(6)
            self.header_box.set_margin_start(12)
            self.header_box.set_margin_end(12)

            # Prefix box (left side)
            self.prefix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            self.prefix_box.set_spacing(6)
            self.header_box.append(self.prefix_box)

            # Title and subtitle box (center)
            self.title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.title_box.set_hexpand(True)
            self.header_box.append(self.title_box)

            # Suffix box (right side)
            self.suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            self.suffix_box.set_spacing(6)
            self.header_box.append(self.suffix_box)

            # Create expander with custom header
            self.expander = Gtk.Expander()
            self.expander.set_label_widget(self.header_box)
            self.append(self.expander)

            # Content box
            self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.expander.set_child(self.content_box)

            self._title_label = None
            self._subtitle_label = None

        def set_title(self, title):
            if self._title_label is None:
                self._title_label = Gtk.Label()
                self._title_label.set_halign(Gtk.Align.START)
                self.title_box.append(self._title_label)
            self._title_label.set_text(title)

        def set_subtitle(self, subtitle):
            if self._subtitle_label is None:
                self._subtitle_label = Gtk.Label()
                self._subtitle_label.set_halign(Gtk.Align.START)
                self._subtitle_label.add_css_class("dim-label")
                self.title_box.append(self._subtitle_label)
            self._subtitle_label.set_text(subtitle)

        def add_prefix(self, widget):
            self.prefix_box.append(widget)

        def add_suffix(self, widget):
            self.suffix_box.append(widget)

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

        def get_subtitle(self):
            return self._subtitle_label.get_text() if self._subtitle_label else ""

        def connect(self, signal_name, callback):
            if signal_name in ('notify::expanded', 'notify::enable-expansion'):
                return self.expander.connect(signal_name, callback)
            return super().connect(signal_name, callback)

    class SpinRow(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
            self.set_spacing(12)
            self.set_margin_top(6)
            self.set_margin_bottom(6)
            self.set_margin_start(12)
            self.set_margin_end(12)
            self._title_box = None
            self._title_label = None
            self._subtitle_label = None
            self.spin = Gtk.SpinButton()
            self.spin.set_valign(Gtk.Align.CENTER)
            self.append(self.spin)

        def set_title(self, title):
            if self._title_box is None:
                self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                self._title_box.set_hexpand(True)
                self._title_label = Gtk.Label(label=title)
                self._title_label.set_halign(Gtk.Align.START)
                self._title_box.append(self._title_label)
                self.prepend(self._title_box)
            else:
                self._title_label.set_text(title)

        def set_subtitle(self, subtitle):
            if self._title_box is None:
                self.set_title("")  # Initialize title box
            if self._subtitle_label is None:
                self._subtitle_label = Gtk.Label(label=subtitle)
                self._subtitle_label.set_halign(Gtk.Align.START)
                self._subtitle_label.add_css_class("dim-label")
                self._title_box.append(self._subtitle_label)
            else:
                self._subtitle_label.set_text(subtitle)

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

    class ComboRow(Gtk.Box):
        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
            self.set_spacing(12)
            self.set_margin_top(6)
            self.set_margin_bottom(6)
            self.set_margin_start(12)
            self.set_margin_end(12)
            self._title_box = None
            self._title_label = None
            self._subtitle_label = None
            self.combo = Gtk.ComboBoxText()
            self.combo.set_valign(Gtk.Align.CENTER)
            super().append(self.combo)  # Use super().append() to avoid conflict

        def set_title(self, title):
            if self._title_box is None:
                self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                self._title_box.set_hexpand(True)
                self._title_label = Gtk.Label(label=title)
                self._title_label.set_halign(Gtk.Align.START)
                self._title_box.append(self._title_label)
                self.prepend(self._title_box)
            else:
                self._title_label.set_text(title)

        def set_subtitle(self, subtitle):
            if self._title_box is None:
                self.set_title("")  # Initialize title box
            if self._subtitle_label is None:
                self._subtitle_label = Gtk.Label(label=subtitle)
                self._subtitle_label.set_halign(Gtk.Align.START)
                self._subtitle_label.add_css_class("dim-label")
                self._title_box.append(self._subtitle_label)
            else:
                self._subtitle_label.set_text(subtitle)

        def append(self, id, label):
            # Forward to the combo widget
            self.combo.append(id, label)

        def get_active_id(self):
            return self.combo.get_active_id()

        def set_active_id(self, id):
            self.combo.set_active_id(id)

        def set_model(self, model):
            # Adw.ComboRow uses set_model with a StringList
            # We'll convert it to ComboBoxText compatible format
            # Clear existing items
            while self.combo.get_active() >= 0 or self.combo.get_has_entry():
                try:
                    self.combo.remove(0)
                except Exception:
                    break

            # Add new items
            for i in range(model.get_n_items()):
                item = model.get_string(i)
                self.combo.append(str(i), item)

        def get_selected(self):
            active = self.combo.get_active()
            return active if active >= 0 else 0

        def set_selected(self, index):
            if index >= 0:
                self.combo.set_active(index)

    class AlertDialog(Gtk.Dialog):
        @staticmethod
        def new(title, message):
            dialog = AlertDialog()
            dialog._title = title
            dialog._message = message
            dialog.set_title(title)

            # Create content area with message
            content = dialog.get_content_area()
            label = Gtk.Label(label=message)
            label.set_wrap(True)
            label.set_margin_top(12)
            label.set_margin_bottom(12)
            label.set_margin_start(12)
            label.set_margin_end(12)
            content.append(label)

            return dialog

        def add_response(self, response_id, label):
            self.add_button(label, response_id)

        def set_response_appearance(self, response_id, appearance):
            pass  # Not supported in Gtk-only mode

        def set_close_response(self, response_id):
            self.set_default_response(response_id)

        def choose(self, parent, cancellable, callback, user_data=None):
            # Adw.AlertDialog uses async choose(), but Gtk.Dialog uses run()
            # We need to convert this to the callback pattern
            self.set_transient_for(parent)
            self.set_modal(True)

            def on_response(dialog, response):
                callback(dialog, None)  # GAsyncResult is None for sync operations

            self.connect('response', on_response)
            self.present()

    class ResponseAppearance:
        DESTRUCTIVE = None

    class AboutWindow(Gtk.AboutDialog):
        def __init__(self, **kwargs):
            super().__init__()
            # Map Adw.AboutWindow parameters to Gtk.AboutDialog
            if 'transient_for' in kwargs:
                self.set_transient_for(kwargs['transient_for'])
            if 'application_name' in kwargs:
                self.set_program_name(kwargs['application_name'])
            if 'application_icon' in kwargs:
                self.set_logo_icon_name(kwargs['application_icon'])
            if 'version' in kwargs:
                self.set_version(kwargs['version'])
            if 'developer_name' in kwargs:
                self.set_authors([kwargs['developer_name']])
            if 'copyright' in kwargs:
                self.set_copyright(kwargs['copyright'])
            if 'license_type' in kwargs:
                self.set_license_type(kwargs['license_type'])

        def set_website(self, url):
            super().set_website(url)

        def set_issue_url(self, url):
            # Gtk.AboutDialog doesn't have set_issue_url, ignore it
            pass

Adw = MockAdw()

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

    # Detect Steam Deck / SteamOS Game Mode or Desktop Mode
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
            /* GNOME Libadwaita Card & Pill Styling */
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
            /* SteamOS Game Mode & Handheld Touch Optimization */
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
        # Non-GNOME Traditional Desktop Window Styling (KDE, XFCE, Cinnamon, MATE, Generic)
        de_label = "KDE Breeze" if de == 'KDE' else f"{de} Traditional Desktop"
        border_rad = "4px" if de == 'KDE' else "2px"
        css_snippets.append(f"""
            /* {de_label} - Traditional Window Styling (Non-GNOME Card Overrides) */
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
