# vf-clamp RoboFont extension entry point — opens the controller window when run from the menu.

# RoboFont executes this file directly when the menu item is triggered (path-style addToMenu).
# Importing and calling the controller here ensures a window opens on each invocation.
from vfClamp.controller import VFClampController
VFClampController()
