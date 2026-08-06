# Bundled game

`gameshell.sh` is a self-extracting **GameShell** archive (v0.6.0-39-g53f470d)
carrying the 45 missions this tutor is written for. It is bundled so that a
fresh clone can play immediately, with no separate download.

GameShell is by Pierre Hyvernat and contributors, released under the GPLv3,
the same licence as this repository. See the upstream project for the engine's
own history and documentation: https://github.com/phyver/GameShell

`play.sh` never plays this file directly. It copies it to `.game/` (ignored by
git) first, because `install.sh` patches archives in place and the engine
rewrites them on every autosave.

To use a different GameShell instead, pass it explicitly:

    ./play.sh /path/to/your/gameshell.sh
