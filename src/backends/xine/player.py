import sys
import os
import md5
import struct

import kaa
import kaa.notifier
import kaa.shm
import kaa.xine as xine

# player skeleton
from kaa.player.skeleton import *

BUFFER_UNLOCKED = 0x10
BUFFER_LOCKED = 0x20

class Xine(MediaPlayer):

    _instance_count = 0

    def __init__(self):
        super(Xine, self).__init__()
        self._instance_id = "kaaxine-%d-%d" % (os.getpid(), Xine._instance_count)
        Xine._instance_count += 1

        self._osd_shmkey = int(md5.md5(self._instance_id + "osd").hexdigest()[:7], 16)
        self._frame_shmkey = int(md5.md5(self._instance_id + "frame").hexdigest()[:7], 16)
        self._osd_shmem = self._frame_shmem = None
        #kaa.signals["shutdown"].connect_weak(self._remove_shmem)
        self._check_new_frame_timer = kaa.notifier.WeakTimer(self._check_new_frame)

        self._is_in_menu = False
        self._state_data = None
        self._stream_info = {}
        self._position = 0.0
        self._cur_frame_output_mode = [True, False, None] # vo, shmem, size

        self._spawn()


    def _remove_shmem(self):
        if self._osd_shmem:
            try:
                self._osd_shmem.detach()
                kaa.shm.remove_memory(self._osd_shmem.shmid)
            except kaa.shm.error:
                # Probably already deleted by child
                pass
            self._osd_shmem = None

        if self._frame_shmem:
            try:
                self._frame_shmem.detach()
                kaa.shm.remove_memory(self._frame_shmem.shmid)
            except kaa.shm.error:
                pass
            self._frame_shmem = None


    def _spawn(self):
        # Launch self (-u is unbuffered stdout)
        script = os.path.join(os.path.dirname(__file__), 'child.py')
        self._process = kaa.notifier.Process("%s -u %s" % (sys.executable, script))
        self._process.signals["stdout"].connect_weak(self._handle_line)
        self._process.signals["stderr"].connect_weak(self._handle_line)
        self._process.signals["completed"].connect_weak(self._exited)
        self._process.set_stop_command(kaa.notifier.WeakCallback(self._end_child))
        self._process.start(str(self._instance_id))



    def rpc(self, command, *args, **kwargs):
        s = repr((command, args, kwargs))
        self._process.write(s + "\n")


    def _end_child(self):
        self.rpc("die")


    def die(self):
        if self._process:
            self._process.stop()


    def _exited(self, exitcode):
        self._state = STATE_NOT_RUNNING
        self._remove_shmem()
        self.signals["quit"].emit()



    # #############################################################################
    # Commands from child
    # #############################################################################

    def _handle_line(self, line):
        if line and line[0] == "!":
            command, args, kwargs = eval(line[1:])
            getattr(self, "_handle_command_" + command)(*args, **kwargs)
        else:
            print "CHILD[%d]: %s" % (self._process.child.pid, line)


    def _handle_command_status(self, pos, time, length, status, speed):
        old_pos = self._position
        self._position = float(time)
        self._stream_info["length"] = length

        if status == 2:
            if self.get_state() not in (STATE_PAUSED, STATE_PLAYING):
                self.signals["start"].emit()
            if speed == xine.SPEED_PAUSE and self.get_state() != STATE_PAUSED:
                self._state = STATE_PAUSED
                self.signals["pause_toggle"].emit()
                self.signals["pause"].emit()
            elif speed > xine.SPEED_PAUSE and self.get_state() != STATE_PLAYING:
                prev_state = self.get_state()
                self._state = STATE_PLAYING
                if prev_state == STATE_PAUSED:
                    self.signals["pause_toggle"].emit()
                self.signals["play"].emit()

            if self._position - old_pos < 0 or self._position - old_pos > 1:
                self.signals["seek"].emit(self._position)
        elif status in (0, 1):
            if self.get_state() in (STATE_PAUSED, STATE_PLAYING):
                # Stream ended.
                self._state = STATE_IDLE
                self.signals["end"].emit()


    def _handle_command_osd_configure(self, width, height, aspect):
        if not self._osd_shmem:
            shmid = kaa.shm.getshmid(self._osd_shmkey)
            if shmid:
                self._osd_shmem = kaa.shm.memory(shmid)
                self._osd_shmem.attach()
        if not self._frame_shmem:
            shmid = kaa.shm.getshmid(self._frame_shmkey)
            if shmid:
                self._frame_shmem = kaa.shm.memory(shmid)
                self._frame_shmem.attach()

        # TODO: remember these values and emit them to new connections to
        # this signal after this point.
        self.signals["osd_configure"].emit(width, height, self._osd_shmem.addr + 16,
                                           width, height)


    def _handle_command_resize(self, size):
        pass
        #self._window.resize(size)


    def _handle_command_stream_info(self, info):
        changed = info != self._stream_info
        self._stream_info = info

        if self._state == STATE_OPENING:
            self._state = STATE_IDLE
            self.rpc("play")
            self.set_frame_output_mode()

        if changed:
            self.signals["stream_changed"].emit()

    def _handle_command_xine_event(self, event, data):
        if event == xine.EVENT_UI_NUM_BUTTONS:
            self._is_in_menu = data["num_buttons"] > 0


    # #############################################################################
    # Window handling
    # #############################################################################


    def _window_visibility_event(self):
        self.rpc("window_changed", self._window.get_id(), self._window.get_size(),
                 self._window.get_visible(), [])

    def _window_expose_event(self, regions):
        self.rpc("window_changed", self._window.get_id(), self._window.get_size(),
                 self._window.get_visible(), regions)

    def _window_configure_event(self, pos, size):
        self.rpc("window_changed", self._window.get_id(), size,
                 self._window.get_visible(), [])



    # #############################################################################
    # Public API
    # #############################################################################


    def open(self, mrl):
        scheme, path = parse_mrl(mrl)
        if scheme not in self.get_supported_schemes():
            raise ValueError, "Unsupported mrl scheme '%s'" % scheme

        self._mrl = "%s:%s" % (scheme, path)
        # Open with kaa.metadata
        self.signals["open"].emit()


    def play(self, video=True):
        if self.get_state() == STATE_PAUSED:
            self.rpc("play")
            return

        wid = None
        if self._window:
            wid = self._window.get_id()
        self.rpc("setup", wid=wid)

        self._position = 0.0
        self.rpc("open", self._mrl)
        self._state = STATE_OPENING


    def pause(self):
        self.rpc("pause")


    def pause_toggle(self):
        if self.get_state() == STATE_PLAYING:
            self.pause()
        else:
            self.play()

    def stop(self):
        self.rpc("stop")


    def seek_relative(self, offset):
        self.rpc("seek", 0, offset)


    def seek_absolute(self, position):
        self.rpc("seek", 1, position)


    def seek_percentage(self, percent):
        pos = (percent / 100.0) * 65535
        self.rpc("seek", 2, pos)

    def get_info(self):
        return self._stream_info


    def osd_update(self, alpha = None, visible = None, invalid_regions = None):
        self.rpc("osd_update", alpha, visible, invalid_regions)


    def osd_can_update(self):
        if not self._osd_shmem:
            return False

        try:
            if ord(self._osd_shmem.read(1)) == BUFFER_UNLOCKED:
                return True
        except kaa.shm.error:
            self._osd_shmem.detach()
            self._osd_shmem = None

        return False

    def set_frame_output_mode(self, vo = None, notify = None, size = None):
        if vo != None:
            self._cur_frame_output_mode[0] = vo
        if notify != None:
            self._cur_frame_output_mode[1] = notify
        if size != None:
            self._cur_frame_output_mode[2] = size

        if self.get_state() in (STATE_NOT_RUNNING, STATE_OPENING):
            return

        vo, notify, size = self._cur_frame_output_mode

        if notify:
            self._check_new_frame_timer.start(0.01)
        else:
            self._check_new_frame_timer.stop()

        self.rpc("frame_output", vo, notify, size)


    def set_window(self, window):
        old_window = self._window
        super(Xine, self).set_window(window)

        if old_window and old_window != self._window:
            # Disconnect signals from existing window.
            old_window.signals["configure_event"].disconnect(self._window_configure_event)
            old_window.signals["map_event"].disconnect(self._window_visibility_event)
            old_window.signals["unmap_event"].disconnect(self._window_visibility_event)
            old_window.signals["expose_event"].disconnect(self._window_expose_event)

        if window and window.signals and old_window != self._window:
            window.signals["configure_event"].connect_weak(self._window_configure_event)
            window.signals["map_event"].connect_weak(self._window_visibility_event)
            window.signals["unmap_event"].connect_weak(self._window_visibility_event)
            window.signals["expose_event"].connect_weak(self._window_expose_event)

        # Sends a window_changed command to slave.
        self._window_visibility_event()


    def get_position(self):
        return self._position

    def get_player_id(self):
        return "xine"


    def _check_new_frame(self):
        if not self._frame_shmem:
            return

        try:
            lock, width, height, aspect = struct.unpack("hhhd", self._frame_shmem.read(16))
        except kaa.shm.error:
            self._frame_shmem.detach()
            self._frame_shmem = None
            return

        if lock & BUFFER_UNLOCKED:
            return

        if width > 0 and height > 0 and aspect > 0:
            self.signals["frame"].emit(width, height, aspect, self._frame_shmem.addr + 16, "bgr32")


    def unlock_frame_buffer(self):
        try:
            self._frame_shmem.write(chr(BUFFER_UNLOCKED))
        except kaa.shm.error:
            self._frame_shmem.detach()
            self._frame_shmem = None


    def nav_command(self, input):
        map = {
            "up": xine.EVENT_INPUT_UP,
            "down": xine.EVENT_INPUT_DOWN,
            "left": xine.EVENT_INPUT_LEFT,
            "right": xine.EVENT_INPUT_RIGHT,
            "select": xine.EVENT_INPUT_SELECT,
            "prev": xine.EVENT_INPUT_PREVIOUS,
            "next": xine.EVENT_INPUT_NEXT,
            "angle_prev": xine.EVENT_INPUT_ANGLE_PREVIOUS,
            "angle_next": xine.EVENT_INPUT_ANGLE_NEXT,
            "menu1": xine.EVENT_INPUT_MENU1,
            "menu2": xine.EVENT_INPUT_MENU2,
            "menu3": xine.EVENT_INPUT_MENU3,
            "menu4": xine.EVENT_INPUT_MENU4,
            "0": xine.EVENT_INPUT_NUMBER_0,
            "1": xine.EVENT_INPUT_NUMBER_1,
            "2": xine.EVENT_INPUT_NUMBER_2,
            "3": xine.EVENT_INPUT_NUMBER_3,
            "4": xine.EVENT_INPUT_NUMBER_4,
            "5": xine.EVENT_INPUT_NUMBER_5,
            "6": xine.EVENT_INPUT_NUMBER_6,
            "7": xine.EVENT_INPUT_NUMBER_7,
            "8": xine.EVENT_INPUT_NUMBER_8,
            "9": xine.EVENT_INPUT_NUMBER_9
        }
        if input in map:
            self.rpc("input", map[input])


    def is_in_menu(self):
        return self._is_in_menu
