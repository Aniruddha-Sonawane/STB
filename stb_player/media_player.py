"""
STB Media Player - Tata Sky inspired.

Keyboard controls
  0-9        type channel number
  Enter      confirm channel / play highlighted program
  Left/Right browse channels while video keeps playing
  Up/Down    navigate EPG rows
  I          toggle EPG
  M          toggle mail indicator
  Vol keys   show volume bar
  Escape     reset EPG row or quit
"""

from stb_player.mixins.base import BaseMixin
from stb_player.mixins.playback import PlaybackMixin
from stb_player.mixins.ui import UiMixin
from stb_player.mixins.youtube import YoutubeMixin


class MediaPlayer(BaseMixin, UiMixin, PlaybackMixin, YoutubeMixin):
    pass
