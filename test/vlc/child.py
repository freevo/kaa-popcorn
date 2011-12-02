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
        self.event_manager = None

    def eventhandler(self, event):
        if event.type == vlc.EventType.MediaPlayerPositionChanged:
            print self.player.get_length() * event.u.new_position / 1000, 'sec'
        if event.type == vlc.EventType.MediaPlayerEndReached:
            kaa.MainThreadCallable(self.stop)()
        if event.type == vlc.EventType.MediaPlayerStopped:
            del self.player
            del self.media
            del self.instance

    def set_window(self, wid):
        self.player.set_xwindow(wid)

    def play(self):
        if not self.event_manager:
            self.event_manager = self.player.event_manager()
            self.event_manager.event_attach(vlc.EventType.MediaPlayerEndReached, self.eventhandler)
            self.event_manager.event_attach(vlc.EventType.MediaPlayerPositionChanged, self.eventhandler)
            self.event_manager.event_attach(vlc.EventType.MediaPlayerStopped, self.eventhandler)
        self.player.play()

    def stop(self):
        self.player.stop()

    def pause(self):
        self.player.pause()

    def seek(self, diff):
        if not self.player.get_length():
            return False
        percent = self.player.get_position()
        if percent < 0:
            percent = 0
        self.player.set_position((diff * 1000.0) / self.player.get_length())

@kaa.coroutine()
def main():
    disp = kaa.display.X11Window(size = (800, 600), title = "Kaa Display Test")
    disp.show()

    player = VLC()
    player.open(sys.argv[1])
    player.set_window(disp.id)

    player.play()

    yield kaa.delay(1)
    player.seek(85)
    yield kaa.delay(10)
    player.stop()
    yield kaa.delay(1)

main()
kaa.main.run()
