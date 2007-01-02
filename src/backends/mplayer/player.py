# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# mplayer/player.py - mplayer backend
# -----------------------------------------------------------------------------
# $Id$
#
# -----------------------------------------------------------------------------
# kaa.popcorn - Generic Player API
# Copyright (C) 2006 Jason Tackaberry, Dirk Meyer
#
# Please see the file AUTHORS for a complete list of authors.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MER-
# CHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#
# -----------------------------------------------------------------------------

# python imports
import logging
import os
import re
import string
import tempfile
import stat
import threading
import struct

# kaa imports
import kaa
from kaa import shm
import kaa.utils
import kaa.notifier
import kaa.display

# kaa.popcorn base imports
from kaa.popcorn.backends.base import MediaPlayer
from kaa.popcorn.ptypes import *

# start mplayer in gdb for debugging
USE_GDB = False

BUFFER_UNLOCKED = 0x10
BUFFER_LOCKED = 0x20

# get logging object
log = logging.getLogger('popcorn.mplayer')
childlog = logging.getLogger('popcorn.child').debug

# A cache holding values specific to an MPlayer executable (version,
# filter list, video/audio driver list, input keylist).  This dict is
# keyed on the full path of the MPlayer binary.
_cache = {}

def _get_mplayer_info(path, callback = None, mtime = None):
    """
    Fetches info about the given MPlayer executable.  If the values are
    cached and the cache is fresh, it returns a dict immediately.  If it
    needs to load MPlayer to fetch the values and callback is specified,
    it does so in a thread, and calls callback with the results on
    completion.  If callback is None and no information is in the cache,
    this function blocks.

    If 'mtime' is not None, it means we've called ourself as a thread.
    """

    if not mtime:
        # Fetch the mtime of the binary
        try:
            mtime = os.stat(path)[stat.ST_MTIME]
        except (OSError, TypeError):
            return None

        if path in _cache and _cache[path]["mtime"] == mtime:
            # Cache isn't stale, so return that.
            return _cache[path]

        if callback:
            # We need to run MPlayer to get these values.  Create a signal,
            # call ourself as a thread, and return the signal back to the
            # caller.
            thread = kaa.notifier.Thread(_get_mplayer_info, path, None, mtime)
            # Thread class ensures the callbacks get invoked in the main
            # thread.
            thread.signals["completed"].connect(callback)
            thread.signals["exception"].connect(callback)
            thread.start()
            return None

    # At this point we're running in a thread.
    info = {
        "version": None,
        "mtime": mtime,
        "video_filters": {},
        "video_drivers": {},
        "audio_filters": {},
        "audio_drivers": {},
        "keylist": []
    }

    groups = {
        'video_filters': ('Available video filters', r'\s*(\w+)\s+:\s+(.*)'),
        'video_drivers': ('Available video output', r'\s*(\w+)\s+(.*)'),
        'audio_filters': ('Available audio filters', r'\s*(\w+)\s+:\s+(.*)'),
        'audio_drivers': ('Available audio output', r'\s*(\w+)\s+(.*)')
    }
    curgroup = None
    for line in os.popen('%s -vf help -af help -vo help -ao help' % path):
        # Check version
        if line.startswith("MPlayer "):
            info['version'] = line.split()[1]
        # Find current group.
        for group, (header, regexp) in groups.items():
            if line.startswith(header):
                curgroup = group
                break
        if not curgroup:
            continue

        # Check regexp
        m = re.match(groups[curgroup][1], line.strip())
        if not m:
            continue

        if len(m.groups()) == 2:
            info[curgroup][m.group(1)] = m.group(2)
        else:
            info[curgroup].append(m.group(1))

    # Another pass for key list.
    for line in os.popen('%s -input keylist' % path):
        # Check regexp
        m = re.match(r'^(\w+)$', line.strip())
        if not m:
            continue
        info['keylist'].append(m.group(1))


    _cache[path] = info
    return info



class MPlayer(MediaPlayer):

    RE_STATUS = re.compile("V:\s*([\d+\.]+)|A:\s*([\d+\.]+)\s\W")
    RE_SWS = re.compile("^SwScaler: [0-9]+x[0-9]+ -> ([0-9]+)x([0-9]+)")

    def __init__(self, config, properties):
        super(MPlayer, self).__init__(config, properties)
        self._state = STATE_NOT_RUNNING
        self._mp_cmd = self._config.mplayer.path
        if not self._mp_cmd:
            self._mp_cmd = kaa.utils.which("mplayer")

        if not self._mp_cmd:
            raise PlayerError, "No MPlayer executable found in PATH"

        self._child_app = None
        self._file = None
        self._file_args = []

        self._filters_pre = []
        self._filters_add = []
        self._last_line = None

        self._mp_info = _get_mplayer_info(self._mp_cmd, self._handle_mp_info)
        self._check_new_frame_t = kaa.notifier.WeakTimer(self._check_new_frame)
        self._cur_outbuf_mode = [True, False, None] # vo, shmem, size


    def __del__(self):
        if self._frame_shmem:
            self._frame_shmem.detach()
        if self._osd_shmem:
            self._osd_shmem.detach()


    def _handle_mp_info(self, info):
        if isinstance(info, Exception):
            self._state = STATE_NOT_RUNNING
            # TODO: handle me
            raise info
        self._mp_info = info



    #
    # child IO
    #

    def _child_stop(self):
        self._child_write("quit")
        # Could be paused, try sending again.
        self._child_write("quit")


    def _child_handle_line(self, line):
        if re.search("@@@|outbuf|overlay", line, re.I):
            childlog(line)
        elif line[:2] not in ("A:", "V:"):
            childlog(line)
        elif USE_GDB:
            childlog(line)

        if line.startswith("V:") or line.startswith("A:"):
            m = MPlayer.RE_STATUS.search(line)
            if m:
                old_pos = self._position
                p = (m.group(1) or m.group(2)).replace(",", ".")
                self._position = float(p)
                # if self._position - old_pos < 0 or \
                # self._position - old_pos > 1:
                # self.signals["seek"].emit(self._position)

                # XXX this logic won't work with seek-while-paused patch; state
                # will be "playing" after a seek.
                if self._state == STATE_PAUSED:
                    self._state = STATE_PLAYING
                if self._state == STATE_OPEN:
                    self.set_frame_output_mode()
                    self._state = STATE_PLAYING
                    self.signals["stream_changed"].emit()

        elif line.startswith("  =====  PAUSE"):
            self._state = STATE_PAUSED

        elif line.startswith("ID_") and line.find("=") != -1:
            attr, value = line.split("=")
            attr = attr[3:]
            info = { "VIDEO_FORMAT": ("vfourcc", str),
                     "VIDEO_BITRATE": ("vbitrate", int),
                     "VIDEO_WIDTH": ("width", int),
                     "VIDEO_HEIGHT": ("height", int),
                     "VIDEO_FPS": ("fps", float),
                     "VIDEO_ASPECT": ("aspect", float),
                     "AUDIO_FORMAT": ("afourcc", str),
                     "AUDIO_CODEC": ("acodec", str),
                     "AUDIO_BITRATE": ("abitrate", int),
                     "AUDIO_NCH": ("channels", int),
                     "LENGTH": ("length", float),
                     "FILENAME": ("filename", str) }
            if attr in info:
                self._streaminfo[info[attr][0]] = info[attr][1](value)

        elif line.startswith("Movie-Aspect"):
            aspect = line[16:].split(":")[0].replace(",", ".")
            if aspect[0].isdigit():
                self._streaminfo["aspect"] = float(aspect)

        elif line.startswith("VO:"):
            m = re.search("=> (\d+)x(\d+)", line)
            if m:
                vo_w, vo_h = int(m.group(1)), int(m.group(2))
                if "aspect" not in self._streaminfo or \
                       self._streaminfo["aspect"] == 0:
                    # No aspect defined, so base it on vo size.
                    self._streaminfo["aspect"] = vo_w / float(vo_h)

        elif line.startswith("overlay:") and line.find("reusing") == -1:
            m = re.search("(\d+)x(\d+)", line)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
                try:
                    if self._osd_shmem:
                        self._osd_shmem.detach()
                except shm.error:
                    pass
                self._osd_shmem = shm.memory(\
                    shm.getshmid(self._osd_shmkey))
                self._osd_shmem.attach()

                self.signals["osd_configure"].emit(\
                    width, height, self._osd_shmem.addr + 16, width, height)

        elif line.startswith("outbuf:") and line.find("shmem key") != -1:
            try:
                if self._frame_shmem:
                    self._frame_shmem.detach()
            except shm.error:
                pass
            self._frame_shmem = shm.memory(shm.getshmid(self._frame_shmkey))
            self._frame_shmem.attach()
            self.set_frame_output_mode()  # Sync

        elif line.startswith("EOF code"):
            if self._state in (STATE_PLAYING, STATE_PAUSED):
                # The player may be idle bow, but we can't set the
                # state. If we do, generic will start a new file while
                # the mplayer process is still running and that does
                # not work. Unless we reuse mplayer proccesses we
                # don't react on EOF and only handle the dead
                # proccess.
                # self._state = STATE_IDLE
                pass
            
        elif line.startswith("Parsing input") and self._window and \
                 self._state == STATE_OPEN:
            # Delete the temporary key input file.
            file = line[line.find("file")+5:]
            os.unlink(file)

        elif line.startswith("FATAL:"):
            log.error(line.strip())

        elif USE_GDB and line.startswith("Program received signal SIGSEGV"):
            # Mplayer crashed, issue backtrace.
            self._child_app.write("thread apply all bt\n")

        if line.strip():
            self._last_line = line


    def _child_write(self, cmd):
        if not self._child_is_alive():
            return False
        log.info('mplayer send %s', cmd)
        self._child_app.write(cmd + "\n")


    def _child_exited(self, exitcode):
        log.info('mplayer exited')
        self._state = STATE_NOT_RUNNING


    def _child_is_alive(self):
        return self._child_app and self._child_app.is_alive()



    #
    # Methods for MediaPlayer subclasses
    #

    def open(self, media):
        """
        Open media.
        """
        if self.get_state() != STATE_NOT_RUNNING:
            raise RuntimeError('mplayer not in STATE_NOT_RUNNING')

        self._file_args = []
        if media.scheme == "dvd":
            file, title = re.search("(.*?)(\/\d+)?$", media.url[4:]).groups()
            if file.replace('/', ''):
                if not os.path.isfile(file):
                    raise ValueError, "Invalid ISO file: %s" % file
                self._file_args.append("-dvd-device \"%s\"" % file)

            self._file = "dvd://"
            if title:
                self._file += title[1:]
        else:
            self._file = media.url

        self._state = STATE_OPENING

        # We have a problem at this point. The 'open' function is used to
        # open the stream and provide information about it. After that, the
        # caller can still change stuff before calling play. Mplayer doesn't
        # work that way so we have to run mplayer with -identify first.
        args = "-nolirc -nojoystick -nomouseinput -identify " +\
               "-vo null -ao null -frames 0"
        ident = kaa.notifier.Process(self._mp_cmd)
        ident.start(args.split(' ') + [ self._file ])
        ident.signals["stdout"].connect_weak(self._child_handle_line)
        ident.signals["stderr"].connect_weak(self._child_handle_line)
        ident.signals["completed"].connect_weak(self._ident_exited)


    def _ident_exited(self, code):
        """
        mplayer -identify finished
        """
        self._state = STATE_OPEN


    def play(self):
        """
        Start playback.
        """
        log.debug('mplayer start playback')

        # we know that self._mp_info has to be there or the object would
        # not be selected by the generic one. FIXME: verify that!
        assert(self._mp_info)

        filters = self._filters_pre[:]
        if 'outbuf' in self._mp_info['video_filters']:
            filters += ["outbuf=%s:yv12" % self._frame_shmkey]


        if self._window:
            # FIXME: add software scaler
            # FIXME: add support for self._config.widescreen
            aspect, size = self._get_aspect()

            # FIXME: this only works if the window has the the aspect
            # as the full screen
            filters.append('expand=:::::%s/%s' % tuple(aspect))
            filters.append('dsize=%s/%s' % size)

        # FIXME: check freevo filter list and add stuff like pp

        filters += self._filters_add
        if 'overlay' in self._mp_info['video_filters']:
            filters += ["overlay=%s" % self._osd_shmkey]

        args = [ "-v", "-slave", "-osdlevel", "0", "-nolirc", "-nojoystick", \
                 "-nomouseinput", "-nodouble", "-fixed-vo", "-identify", \
                 "-framedrop" ]

        if filters:
            args.extend(("-vf", ",".join(filters)))

        if isinstance(self._window, kaa.display.X11Window):
            args.extend((
                "-wid", hex(self._window.get_id()),
                "-display", self._window.get_display().get_string()))
        else:
            # no window == no video out
            args.extend(('-vo', 'null'))

        if self._config.audio.passthrough:
            args += [ '-ac', 'hwac3,hwdts,' ]
        else:
            args += [ '-channels', str(self._config.audio.channels) ]
            
        args += [ '-ao', self._config.audio.driver ]
        if self._config.audio.driver == 'alsa':
            args[-1] += ":noblock"
            n_channels = self._streaminfo.get('channels')
            if self._streaminfo.get('acodec') in ('a52', 'hwac3', 'ffdts', 'hwdts'):
                device = self._config.audio.device.passthrough
            elif n_channels == 1:
                device = self._config.audio.device.mono
            elif n_channels <= 4:
                device = self._config.audio.device.surround40
            elif n_channels <= 6:
                device = self._config.audio.device.surround51
            else:
                device = self._config.audio.device.stereo
            if device != '':
                args[-1] += ':device=' + device.replace(':', '=')
            
        # There is no way to make MPlayer ignore keys from the X11 window.  So
        # this hack makes a temp input file that maps all keys to a dummy (and
        # non-existent) command which causes MPlayer not to react to any key
        # presses, allowing us to implement our own handlers.  The temp file is
        # deleted once MPlayer has read it.
        keys = filter(lambda x: x not in string.whitespace, string.printable)
        keys = list(keys) + self._mp_info["keylist"]
        fp, keyfile = tempfile.mkstemp()
        for key in keys:
            os.write(fp, "%s noop\n" % key)
        os.close(fp)
        args.extend(('-input', 'conf=%s' % keyfile))

        if self._file_args:
            if isinstance(self._file_args, str):
                args.extend(self._file_args.split(' '))
            else:
                args.extend(self._file_args)

        if self._file:
            args.append(self._file)

        log.info("spawn: %s %s", self._mp_cmd, ' '.join(args))

        if USE_GDB:
            self._child_app = kaa.notifier.Process("gdb")
            self._child_app.start(self._mp_cmd)
            self._child_app.write("run %s\n" % ' '.join(args))
        else:
            self._child_app = kaa.notifier.Process(self._mp_cmd)
            self._child_app.start(args)

        self._child_app.signals["stdout"].connect_weak(self._child_handle_line)
        self._child_app.signals["stderr"].connect_weak(self._child_handle_line)
        self._child_app.signals["completed"].connect_weak(self._child_exited)
        stop = kaa.notifier.WeakCallback(self._child_stop)
        self._child_app.set_stop_command(stop)
        return


    def stop(self):
        """
        Stop playback.
        """
        if self._child_app:
            self._child_app.stop()
            self._state = STATE_SHUTDOWN


    def pause(self):
        """
        Pause playback.
        """
        self._child_write("pause")


    def resume(self):
        """
        Resume playback.
        """
        self._child_write("pause")


    def seek(self, value, type):
        """
        SEEK_RELATIVE, SEEK_ABSOLUTE and SEEK_PERCENTAGE.
        """
        s = [SEEK_RELATIVE, SEEK_PERCENTAGE, SEEK_ABSOLUTE]
        self._child_write("seek %f %s" % (value, s.index(type)))


    def _prop_audio_delay(self, delay):
        """
        Sets audio delay. Positive value defers audio by delay.
        """
        self._child_write("audio_delay %f 1" % -delay)


    #
    # Methods for filter handling (not yet in generic and base
    #

    def prepend_filter(self, filter):
        """
        Add filter to the prepend list.
        """
        self._filters_pre.append(filter)


    def append_filter(self, filter):
        """
        Add filter to the normal filter list.
        """
        self._filters_add.append(filter)


    def get_filters(self):
        """
        Return all filter set.
        """
        return self._filters_pre + self._filters_add


    def remove_filter(self, filter):
        """
        Remove filter for filter list.
        """
        for l in (self._filters_pre, self._filters_add):
            if filter in l:
                l.remove(filter)


    #
    # Methods and helper for MediaPlayer subclasses for CAP_OSD
    #

    def osd_can_update(self):
        """
        Returns True if it is safe to write to the player's shared memory
        buffer used for OSD, and False otherwise.  If this buffer is written
        to even though this function returns False, the OSD may exhibit
        corrupt output or tearing during animations.
        See generic.osd_can_update for details.
        """
        if not self._osd_shmem:
            return False

        try:
            if ord(self._osd_shmem.read(1)) == BUFFER_UNLOCKED:
                return True
        except shm.error:
            self._osd_shmem.detach()
            self._osd_shmem = None

        return False



    def osd_update(self, alpha = None, visible = None, invalid_regions = None):
        """
        Updates the OSD of the player based on the given argments:
        See generic.osd_update for details.
        """
        cmd = []
        if alpha != None:
            cmd.append("alpha=%d" % alpha)
        if visible != None:
            cmd.append("visible=%d" % int(visible))
        if invalid_regions:
            for (x, y, w, h) in invalid_regions:
                cmd.append("invalidate=%d:%d:%d:%d" % (x, y, w, h))
        self._child_write("overlay %s" % ",".join(cmd))
        self._overlay_set_lock(BUFFER_LOCKED)

        try:
            if ord(self._osd_shmem.read(1)) == BUFFER_UNLOCKED:
                return True
        except shm.error:
            self._osd_shmem.detach()
            self._osd_shmem = None

        return False


    def _overlay_set_lock(self, byte):
        try:
            if self._osd_shmem and self._osd_shmem.attached:
                self._osd_shmem.write(chr(byte))
        except shm.error:
            self._osd_shmem.detach()
            self._osd_shmem = None



    #
    # Methods and helper for MediaPlayer subclasses for CAP_CANVAS
    #


    def set_frame_output_mode(self, vo = None, notify = None, size = None):
        """
        Controls if and how frames are delivered via the 'frame' signal, and
        whether or not frames are drawn to the vo driver's video window.
        See generic.set_frame_output_mode for details.
        """
        if vo != None:
            self._cur_outbuf_mode[0] = vo
        if notify != None:
            self._cur_outbuf_mode[1] = notify
            if notify:
                self._check_new_frame_t.start(0.01)
            else:
                self._check_new_frame_t.stop()
        if size != None:
            self._cur_outbuf_mode[2] = size

        if not self._child_is_alive():
            return

        mode = { (False, False): 0, (True, False): 1,
                 (False, True): 2, (True, True): 3 }
        mode = mode[tuple(self._cur_outbuf_mode[:2])]

        size = self._cur_outbuf_mode[2]
        if size == None:
            self._child_write("outbuf %d" % mode)
        else:
            self._child_write("outbuf %d %d %d" % (mode, size[0], size[1]))


    def unlock_frame_buffer(self):
        """
        Unlocks the frame buffer provided by the last 'frame' signal
        See generic.unlock_frame_buffer for details.
        """
        try:
            self._frame_shmem.write(chr(BUFFER_UNLOCKED))
        except shm.error:
            self._frame_shmem.detach()
            self._frame_shmem = None


    def _check_new_frame(self):
        if not self._frame_shmem:
            return

        try:
            lock, width, height, aspect = \
                  struct.unpack("hhhd", self._frame_shmem.read(16))
        except shm.error:
            self._frame_shmem.detach()
            self._frame_shmem = None
            return

        if lock & BUFFER_UNLOCKED:
            return

        if width > 0 and height > 0 and aspect > 0:
            self.signals["frame"].emit(\
                width, height, aspect, self._frame_shmem.addr + 16, "yv12")
