#!/usr/bin/python3 --
import gi
from os import abort
import traceback
import xcffib
import xcffib.xproto
from xcffib.xproto import WindowClass, CW, EventMask

gi.require_version("GLib", "2.0")
from gi.repository import GLib, GObject


class DisgustingX11FullscreenWindowHack:
    """
    GTK3 menus have a horrible bug under Xwayland: if the user clicks on a
    native Wayland surface, the menu is not dismissed.  This class works around
    the problem by using evil X11 hacks, such as a fullscreen override-redirect
    window that is made transparent.
    """

    def __init__(self) -> None:
        self.keep_going = False
        self.runtime_cb = None
        conn = self.conn = xcffib.connect()
        self.source = GLib.unix_fd_source_new(
            conn.get_file_descriptor(),
            GLib.IOCondition(GLib.IO_IN | GLib.IO_HUP | GLib.IO_PRI),
        )
        GObject.source_set_closure(self.source, self.source_callback)
        main_loop = GLib.main_context_ref_thread_default()
        assert main_loop is not None
        self.main_loop = main_loop
        self.source.attach(main_loop)

        setup = conn.get_setup()
        if setup.roots_len != 1:
            raise RuntimeError(
                f"X server has {setup.roots_len} screens, this is not supported"
            )
        (screen,) = setup.roots
        # This is not guaranteed to work, but assume it will.
        # pylint: disable=line-too-long
        (depth_32,) = (depth for depth in screen.allowed_depths if depth.depth == 32)
        for i in depth_32.visuals:
            if (
                i._class == xcffib.xproto.VisualClass.TrueColor
                and i.blue_mask == 0x0000FF
                and i.green_mask == 0x00FF00
                and i.red_mask == 0xFF0000
                and i.colormap_entries == 256
                and i.bits_per_rgb_value == 8
            ):
                visual_32 = i
                break
        else:
            assert False, "no valid visual type"
        self.screen = screen
        self.window_id = conn.generate_id()
        self.colormap_id = conn.generate_id()
        self.gc_id = conn.generate_id()
        proto = self.proto = xcffib.xproto.xprotoExtension(conn)
        assert screen.width_in_pixels > 0
        assert screen.height_in_pixels > 0
        proto.CreateColormap(
            xcffib.XCB_NONE, self.colormap_id, screen.root, visual_32.visual_id
        )
        proto.CreateWindow(
            depth=32,
            wid=self.window_id,
            parent=screen.root,
            x=0,
            y=0,
            width=screen.width_in_pixels,
            height=screen.height_in_pixels,
            border_width=0,
            _class=WindowClass.InputOutput,
            visual=visual_32.visual_id,
            value_mask=CW.BorderPixel
            | CW.OverrideRedirect
            | CW.EventMask
            | CW.Colormap,
            value_list=[
                0,  # Transparent border pixel
                1,  # Is override redirect
                EventMask.ButtonPress,  # Only button press events
                self.colormap_id,  # ID of the colormap
            ],
        )
        proto.CreateGC(self.gc_id, self.window_id, 0, [])

    def __del__(self):
        self.source.remove(self.main_loop)
        self.source.unref()
        self.conn.disconnect()

    def show_for_widget(self, widget) -> None:
        widget.connect("unmap", lambda _unused: self.hide())
        widget.connect("map", lambda _unused: self.show(widget.hide))

    def show(self, on_event) -> None:
        if self.keep_going:
            return
        self.keep_going = True
        self.proto.MapWindow(self.window_id)
        self.proto.PolyFillRectangle(
            self.window_id,
            self.gc_id,
            1,
            [
                xcffib.xproto.RECTANGLE.synthetic(
                    # pylint: disable=line-too-long
                    0,
                    0,
                    self.screen.width_in_pixels,
                    self.screen.height_in_pixels,
                )
            ],
        )
        self.conn.flush()
        self.runtime_cb = on_event
        self.on_event()

    def hide(self) -> None:
        if self.keep_going:
            self.proto.UnmapWindow(self.window_id)
            self.keep_going = False
            self.on_event()

    def on_event(self) -> None:
        self.conn.flush()
        while True:
            event = self.conn.poll_for_event()
            if event is None:
                return
            if isinstance(event, xcffib.xproto.ButtonPressEvent):
                if event.event == self.window_id:
                    self.runtime_cb()
                    self.keep_going = False

    def source_callback(self, fd, flags) -> int:
        try:
            assert fd == self.conn.get_file_descriptor()
            try:
                self.on_event()
            except xcffib.ConnectionException:
                self.keep_going = False
                return GLib.SOURCE_REMOVE
            if flags & GLib.IO_HUP:
                self.keep_going = False
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE
        except BaseException:
            try:
                traceback.print_exc()
            finally:
                abort()


if __name__ == "__main__":
    a = DisgustingX11FullscreenWindowHack()
    _main_loop = GLib.main_context_ref_thread_default()
    a.show(lambda *args, **kwargs: None)
    while a.keep_going:
        _main_loop.iteration()
