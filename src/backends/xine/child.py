import sys
import md5
import gc

import kaa
import kaa.notifier
import kaa.shm
import kaa.xine as xine

from kaa.popcorn.utils import Player
from kaa.popcorn.ptypes import *

BUFFER_UNLOCKED = 0x10
BUFFER_LOCKED = 0x20

class XinePlayerChild(Player):

    def __init__(self, osd_shmkey, frame_shmkey):
        Player.__init__(self)

        self._xine = xine.Xine()
        self._stream = self._vo = self._ao = None
        self._osd_shmkey = int(osd_shmkey)
        self._frame_shmkey = int(frame_shmkey)
        self._osd_shmem = self._frame_shmem = None

        self._x11_window_size = 0, 0
        self._x11_last_aspect = -1
        self._status = kaa.notifier.WeakTimer(self._status_output)
        self._status_last = None

        self._xine.set_config_value("effects.goom.fps", 20)
        self._xine.set_config_value("effects.goom.width", 512)
        self._xine.set_config_value("effects.goom.height", 384)
        self._xine.set_config_value("effects.goom.csc_method", "Slow but looks better")
        self._config = None

    def _check_stream_handles(self):
        """
        Check if stream is ok.
        """
        v_unhandled = self._stream.get_info(xine.STREAM_INFO_HAS_VIDEO) and \
            not self._stream.get_info(xine.STREAM_INFO_IGNORE_VIDEO) and \
            not self._stream.get_info(xine.STREAM_INFO_VIDEO_HANDLED)
        a_unhandled = self._stream.get_info(xine.STREAM_INFO_HAS_AUDIO) and \
            not self._stream.get_info(xine.STREAM_INFO_IGNORE_AUDIO) and \
            not self._stream.get_info(xine.STREAM_INFO_AUDIO_HANDLED)
        return not (v_unhandled or a_unhandled)


    def _status_output(self):
        """
        Outputs stream status information.
        """
        if not self._stream:
            return

        # FIXME: this gets not updated very often, I have no idea why
        t = self._stream.get_pos_length()
        status = self._stream.get_status()
        if status == xine.STATUS_PLAY and None in t:
            # Status is playing, but pos/time is not known for stream,
            # which likely means we have seeked and are not done seeking
            # get, so position is not yet determined.  In this case, don't
            # send a status update to parent yet.
            return

        speed = self._stream.get_parameter(xine.PARAM_SPEED)

        # Line format: pos time length status speed
        # Where status is one of XINE_STATUS_ constants, and speed
        # is one of XINE_SPEED constants.
        cur_status = (t[0], t[1], t[2], status, speed)
        if cur_status != self._status_last:
            self._status_last = cur_status
            self.parent.set_status(*cur_status)


    def _get_streaminfo(self):
        if not self._stream:
            return {}

        info = {
            "vfourcc": self._stream.get_info(xine.STREAM_INFO_VIDEO_FOURCC),
            "afourcc": self._stream.get_info(xine.STREAM_INFO_AUDIO_FOURCC),
            "vcodec": self._stream.get_meta_info(xine.META_INFO_VIDEOCODEC),
            "acodec": self._stream.get_meta_info(xine.META_INFO_AUDIOCODEC),
            "width": self._stream.get_info(xine.STREAM_INFO_VIDEO_WIDTH),
            "height": self._stream.get_info(xine.STREAM_INFO_VIDEO_HEIGHT),
            "aspect": self._stream.get_info(xine.STREAM_INFO_VIDEO_RATIO) / 10000.0,
            "fps": self._stream.get_info(xine.STREAM_INFO_FRAME_DURATION),
            "length": self._stream.get_length(),
        }
        if self._x11_last_aspect != -1:
            # Use the aspect ratio as given to the frame output callback
            # as it tends to be more reliable (particularly for DVDs).
            info["aspect"] = self._x11_last_aspect
        if info["aspect"] == 0 and info["height"] > 0:
            info["aspect"] = info["width"] / float(info["height"])
        if info["fps"]:
            info["fps"] = 90000.0 / info["fps"]
        return info


    # #############################################################################
    # kaa.xine callbacks
    # #############################################################################

    def _x11_frame_output_cb(self, width, height, aspect):
        #print "Frame output", width, height, aspect
        w, h, a = self._xine._get_vo_display_size(width, height, aspect)
        if abs(self._x11_last_aspect - a) > 0.01:
            print "VO: %dx%d -> %dx%d" % (width, height, w, h)
            self.parent.resize((w, h))
            self._x11_last_aspect = a
        if self._x11_window_size != (0, 0):
            w, h = self._x11_window_size
        return (0, 0), (0, 0), (w, h), 1.0


    def _x11_dest_size_cb(self, width, height, aspect):
        # TODO:
        #if not self._x11_window_visibile:
        #    w, h, a = self._get_vo_display_size(width, height, aspect)
        #else:
        #    w, h = self._x11_window_size
        w, h = self._x11_window_size
        return (w, h), 1.0


    def _osd_configure(self, width, height, aspect):
        frame_shmem_size = width * height * 4 + 16
        #if self._frame_shmem and self._frame_shmem.size != frame_shmem_size:
        if not self._frame_shmem:
            self._frame_shmem = kaa.shm.create_memory(self._frame_shmkey, frame_shmem_size)
            self._frame_shmem.attach()
        if not self._osd_shmem:
            self._osd_shmem = kaa.shm.create_memory(self._osd_shmkey, 2000 * 2000 * 4 + 16)
            self._osd_shmem.attach()
            self._osd_shmem.write(chr(BUFFER_UNLOCKED))

        # FIXME: don't hardcode buffer dimensions
        assert(width*height*4 < 2000*2000*4)
        self.parent.osd_configure(width, height, aspect)
        return self._osd_shmem.addr + 16, width * 4, self._frame_shmem.addr


    def handle_xine_event(self, event):
        if len(event.data) > 1:
            del event.data["data"]
        print "EVENT", event.type, event.data
        if event.type == xine.EVENT_UI_CHANNELS_CHANGED:
            self.parent.set_streaminfo(True, self._get_streaminfo())
        self.parent.xine_event(event.type, event.data)


    # #############################################################################
    # Commands from parent process
    # #############################################################################

    def window_changed(self, wid, size, visible, exposed_regions):
        self._x11_window_size = size
        if self._vo:
            self._vo.send_gui_data(xine.GUI_SEND_VIDEOWIN_VISIBLE, visible)
            self._vo.send_gui_data(xine.GUI_SEND_DRAWABLE_CHANGED, wid)


    def set_config(self, config):
        self._config = config

        
    def configure_video(self, wid, aspect):
        """
        Configure video output.
        """
        control_return = []
        self._vo_visible = True
        if wid and isinstance(wid, int):
            self._vo = self._xine.open_video_driver(
                "xv", control_return = control_return, wid = wid,
                osd_configure_cb = kaa.notifier.WeakCallback(self._osd_configure),
                frame_output_cb = kaa.notifier.WeakCallback(self._x11_frame_output_cb),
                dest_size_cb = kaa.notifier.WeakCallback(self._x11_dest_size_cb))
            self._driver_control = None
            
            # This segfaults right now:
            # self._vo = self._xine.open_video_driver(
            #     "kaa", control_return = control_return,
            #     passthrough = "xv", wid = wid,
            #     osd_configure_cb = kaa.notifier.WeakCallback(self._osd_configure),
            #     # osd_buffer = self._osd_shmem.addr + 16, osd_stride = 2000 * 4,
            #     # osd_rows = 2000,
            #     # self._vo = self._xine.open_video_driver("xv", wid = wid,
            #     frame_output_cb = kaa.notifier.WeakCallback(self._x11_frame_output_cb),
            #     dest_size_cb = kaa.notifier.WeakCallback(self._x11_dest_size_cb))
            # self._driver_control = control_return[0]
        elif wid and isinstance(wid, str) and wid.startswith('fb'):
            self._vo = self._xine.open_video_driver(
                "kaa", control_return = control_return,
                passthrough = "vidixfb",
                osd_configure_cb = kaa.notifier.WeakCallback(self._osd_configure),
                frame_output_cb = kaa.notifier.WeakCallback(self._x11_frame_output_cb),
                dest_size_cb = kaa.notifier.WeakCallback(self._x11_dest_size_cb))
            self._driver_control = control_return[0]
        else:
            self._vo = self._xine.open_video_driver("none")
            self._driver_control = None
            self._vo_visible = False
        
        self._expand_post = self._xine.post_init("expand", video_targets = [self._vo])
        if aspect:
            self._expand_post.set_parameters(aspect=aspect)
        self._expand_post.set_parameters(enable_automatic_shift = True)


    def configure_audio(self, driver):
        """
        Configure audio output.
        """
        self._ao = self._xine.open_audio_driver(driver=driver)
        if driver == 'alsa':
            set = self._xine.set_config_value
            dev = self._config.get('audio').get('device').get
            if dev('mono'):
                set('audio.device.alsa_default_device', dev('mono'))
            if dev('stereo'):
                set('audio.device.alsa_front_device', dev('stereo'))
            if dev('surround40'):
                set('audio.device.alsa_surround40_device', dev('surround40'))
            if dev('surround51'):
                set('audio.device.alsa_surround51_device', dev('surround51'))
            if dev('passthrough'):
                set('audio.device.alsa_passthrough_device', dev('passthrough'))
            if self._config['audio']['passthrough']:
                set('audio.output.speaker_arrangement', 'Pass Through')
            else:
                channels = { 2: 'Stereo 2.0', 4: 'Surround 4.0', 6: 'Surround 5.1' }
                num = self._config['audio']['channels']
                set('audio.output.speaker_arrangement', channels[num])
        # FIXME: it should, but a52_pass_through does not exist.
        # We need a way to turn on/off passthrough
        # self._xine.set_config_value('audio.a52_pass_through', 1)

    def configure_stream(self):
        """
        Basic stream setup.
        """
        self._stream = self._xine.new_stream(self._ao, self._vo)
        #self._stream.set_parameter(xine.PARAM_VO_CROP_BOTTOM, 10)
        self._stream.signals["event"].connect_weak(self.handle_xine_event)


        # self._noise_post = self._xine.post_init("noise", video_targets = [self._vo])
        # self._noise_post.set_parameters(luma_strength = 3, quality = "temporal")
        # self._stream.get_video_source().wire(self._noise_post.get_default_input())

        # self._deint_post = self._xine.post_init("tvtime", video_targets = [self._expand_post.get_default_input()])
        # self._deint_post = self._xine.post_init("tvtime", video_targets = [self._vo])
        # self._deint_post.set_parameters(method = config.deinterlacer.method,
        # chroma_filter = config.deinterlacer.chroma_filter)

        self._stream.get_video_source().wire(self._expand_post.get_default_input())

        # self._driver_control("set_passthrough", False)


    def open(self, mrl):
        try:
            self._stream.open(mrl)
            if not self._stream.get_info(xine.STREAM_INFO_HAS_VIDEO) and self._vo_visible:
                self._goom_post = self._xine.post_init("goom", video_targets = [self._vo], audio_targets=[self._ao])

                self._stream.get_audio_source().wire(self._goom_post.get_default_input())
            else:
                self._stream.get_audio_source().wire(self._ao)
            xine._debug_show_chain(self._stream._obj)
        except xine.XineError:
            self.parent.set_streaminfo(False, self._stream.get_error())
            print "Open failed:", self._stream.get_error()
            return
        if not self._check_stream_handles():
            self.parent.set_streaminfo(False, None)
            print "unable to play stream"
            return
        self.parent.set_streaminfo(True, self._get_streaminfo())
        self._status.start(0.001)


    def osd_update(self, alpha, visible, invalid_regions):
        if not self._osd_shmem:
            return

        if alpha != None:
            self._driver_control("set_osd_alpha", alpha)
        if visible != None:
            self._driver_control("set_osd_visibility", visible)
        if invalid_regions != None:
            self._driver_control("osd_invalidate_rect", invalid_regions)
        self._osd_shmem.write(chr(BUFFER_UNLOCKED))


    def play(self):
        status = self._stream.get_status()
        if status == xine.STATUS_STOP:
            self._stream.play()


    def pause(self):
        self._stream.set_parameter(xine.PARAM_SPEED, xine.SPEED_PAUSE)


    def resume(self):
        self._stream.set_parameter(xine.PARAM_SPEED, xine.SPEED_NORMAL)


    def seek(self, value, type):
        if type == SEEK_RELATIVE:
            self._stream.seek_relative(value)
        if type == SEEK_ABSOLUTE:
            self._stream.seek_absolute(value)
        if type == SEEK_PERCENTAGE:
            self._stream.play(pos = (value / 100.0) * 65535)


    def stop(self):
        self._status.stop()
        if self._stream:
            self._stream.stop()
            self._stream.close()
        self.parent.play_stopped()


    def die(self):
        self.stop()
        sys.exit(0)
        

    def frame_output(self, vo, notify, size):
        if not self._driver_control:
            # FIXME: Tack, what am I doing here?
            return
        if vo != None:
            self._driver_control("set_passthrough", vo)
        if notify != None:
            if notify:
                print "DEINTERLACE CHEAP MODE: True"
                self._deint_post.set_parameters(cheap_mode = True)
            else:
                print "DEINTERLACE CHEAP MODE: False"
                self._deint_post.set_parameters(cheap_mode = False)

            self._driver_control("set_notify_frame", notify)
        if size != None:
            self._driver_control("set_notify_frame_size", size)


    def input(self, input):
        self._stream.send_event(input)
