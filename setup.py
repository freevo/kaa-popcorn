# -*- coding: iso-8859-1 -*-
# $Id$
# -----------------------------------------------------------------------------
# setup.py - Setup script for kaa.popcorn
# -----------------------------------------------------------------------------
# kaa.popcorn - Generic Player API
# Copyright (C) 2008 Jason Tackaberry, Dirk Meyer
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
# -----------------------------------------------------------------------------

# python imports
import sys

# We require python 2.5 or later, so complain if that isn't satisfied.
if sys.version.split()[0] < '2.5':
    print "Python 2.5 or later required."
    sys.exit(1)

try:
    # kaa base imports
    from kaa.distribution.core import setup, Extension
except ImportError:
    print 'kaa.base not installed'
    sys.exit(1)
    
ext_modules = []
#libvisual = Extension("kaa.popcorn._libvisual", ['src/extensions/libvisual.c'])
#if libvisual.check_library("libvisual", "0.2.0"):
#    print "+ libvisual support enabled"
#    ext_modules.append(libvisual)
#else:
#    print "- libvisual support disabled"

setup(module = 'popcorn', 
      version = '0.2.0', 
#      scripts = [ 'bin/popcorn' ],
      license = 'GPL',
      summary = 'Media player abstraction library supporting multiple backends',
      rpminfo = {
          'requires':       'python-kaa-base >= 0.1.2, python-kaa-xine >= 0.9.0',
          'build_requires': 'python-kaa-base >= 0.1.2'
      },
      ext_modules = ext_modules)
