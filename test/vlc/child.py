import sys
import time

import kaa.display
import generated_vlc as vlc

class VLC(object):
    
    def open(self, filename):

        self.instance = vlc.Instance()
        self.media = self.instance.media_new(filename)
        self.media.add_option('no-video-title-show')
        self.player = self.instance.media_player_new()
        self.player.set_media(self.media)

    def eventhandler(self, event):
        if event.type == vlc.EventType.MediaPlayerPositionChanged:
            print event.u.new_position

    def set_window(self, wid):
        self.player.set_xwindow(wid)
        
    def play(self):
        self.event_manager = self.player.event_manager()
        # self.event_manager.event_attach(vlc.EventType.MediaPlayerEndReached,      end_callback)
        self.event_manager.event_attach(vlc.EventType.MediaPlayerPositionChanged, self.eventhandler)
        self.player.play()

    def stop(self):
        self.player.stop()

    def pause(self):
        self.player.pause()

disp = kaa.display.X11Window(size = (800, 600), title = "Kaa Display Test")
disp.show()

player = VLC()
player.open(sys.argv[1])
player.set_window(disp.id)


player.play()

time.sleep(3)
player.pause()
time.sleep(1)
player.play()
time.sleep(3)
