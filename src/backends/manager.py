# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# manager - manage the loaded backends
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

__all__ = [ 'register', 'get_player_class', 'get_all_players' ]

# python imports
import os

# kaa.popcorn imports
from kaa.popcorn.ptypes import *
from kaa.popcorn.utils import parse_mrl

# backend imports
from base import MediaPlayer

# internal list of players
_players = {}


def register(player_id, cls, get_caps_callback):
    """
    Register a new player.
    """
    assert(issubclass(cls, MediaPlayer))
    if player_id in _players:
        raise ValueError, "Player '%s' already registered" % name

    # set player id
    cls._player_id = player_id

    # FIXME: we just defer calling get_caps_callback until the first time
    # a player is needed, but we should do this in a thread when the system
    # is idle.
    _players[player_id] = {
        "class": cls,
        "callback": get_caps_callback,
        "loaded": False
    }


def get_player_class(mrl = None, caps = None, exclude = None, force = None,
                     preferred = None):
    """
    Searches the registered players for the most capable player given the mrl
    or required capabilities.  A specific player can be returned by specifying
    the player id.  If exclude is specified, it is a name (or list of names)
    of players to skip (in case one or more players are known not to work with
    the given mrl).  The player's class object is returned if a suitable
    player is found, otherwise None.
    """

    # Ensure all players have their capabilities fetched.
    for player_id in _players:
        if _players[player_id]["loaded"]:
            continue

        player_caps, schemes, exts, codecs = _players[player_id]["callback"]()

        # FIXME: fix the usage of player_caps everywhere to acceped a
        # dict with capabilities and the rating
        player_caps = [ x for x in player_caps.keys() if x ]
        
        _players[player_id].update({
            "caps": player_caps,
            "schemes": schemes,
            # Prefer this player for these extensions.
            "extensions": exts,
            # Prefer this player for these codecs.
            "codecs": codecs,
            "loaded": True,
        })
        cls = _players[player_id]['class']
        cls._player_caps = player_caps
        cls._player_schemes = schemes


    if force == mrl == caps == None:
        if preferred != None and preferred in _players:
            return _players[preferred]["class"]
            
        # FIXME: return the best possible player. This requires a new
        # register function with more information about how good a player
        # is for playing a specific mrl.
        return _players.values()[0]["class"]

    if force != None and force in _players:
        return _players[force]["class"]

    if mrl != None:
        scheme, path = parse_mrl(mrl)
        ext = os.path.splitext(path)[1]
        if ext:
            ext = ext[1:]  # Eat leading '.'

    if caps != None and type(caps) not in (tuple, list):
        caps = (caps,)
    if exclude != None and type(exclude) not in (tuple, list):
        exclude  = (exclude,)

    choice = None
    for player_id, player in _players.items():
        if mrl != None and scheme not in player["schemes"]:
            # MRL scheme is not supported by this player.
            continue
        if exclude and player_id in exclude:
            # Player is in exclude list.
            continue
        if caps != None and not sets.Set(caps).issubset(sets.Set(player["caps"])):
            # Requested capabilities not present.
            continue
        if mrl and choice and ext in choice["extensions"] and ext not in player["extensions"]:
            # Our current choice lists the mrl's extension while this choice 
            # doesn't.
            continue

        if scheme == 'dvd' and choice and CAP_DVD_MENUS in player["caps"] and \
           CAP_DVD_MENUS not in choice["caps"]:
            # If the mrl is dvd, make sure we prefer the player that supports
            # CAP_DVD_MENUS
            choice = player
        elif player_id == preferred or not choice:
            choice = player

    if not choice:
        return None

    return choice["class"]


def get_all_players():
    """
    Return all player id strings.
    """
    return _players.keys()
