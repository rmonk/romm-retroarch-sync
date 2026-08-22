import os
import sys
import time
from gi.repository import Gtk, Gdk, GLib, Gio, GObject, Pango
from ui.compat import Adw, HAS_ADW
from ui.models import GameItem, DiscItem, PlatformItem

class ColumnFactory:
    """Creates and binds GTK ColumnView cell widgets for the games library."""

    def __init__(self, library_section):
        self.library = library_section
        self.parent = library_section.parent

    def setup_checkbox_cell(self, factory, list_item):
        """Setup checkbox column cell widget"""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        check = Gtk.CheckButton()
        box.append(check)
        list_item.set_child(box)

    def setup_platform_cell(self, factory, list_item):
        """Setup platform column cell widget"""
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_valign(Gtk.Align.CENTER)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        list_item.set_child(label)

    def setup_name_cell(self, factory, list_item):
        """Setup game title / collection name cell widget"""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)

        expander = Gtk.TreeExpander()
        box.append(expander)

        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_valign(Gtk.Align.CENTER)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        box.append(label)

        list_item.set_child(box)

    def setup_status_cell(self, factory, list_item):
        """Setup download/sync status cell widget"""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image()
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        box.append(icon)

        label = Gtk.Label()
        label.set_valign(Gtk.Align.CENTER)
        box.append(label)

        list_item.set_child(box)

    def setup_size_cell(self, factory, list_item):
        """Setup file size cell widget"""
        label = Gtk.Label()
        label.set_halign(Gtk.Align.END)
        label.set_valign(Gtk.Align.CENTER)
        list_item.set_child(label)

    def setup_sync_status_cell(self, factory, list_item):
        """Setup collection auto-sync switch/status cell widget"""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        switch = Gtk.Switch()
        switch.set_valign(Gtk.Align.CENTER)
        box.append(switch)

        list_item.set_child(box)
