# vf-clamp RoboFont extension entry point — opens the controller window when run from the menu.

# RoboFont executes this file directly when the menu item is triggered (path-style addToMenu).
# We deliberately do NOT instantiate the controller at import time — otherwise any
# `import vfClamp` (bundle scanning, test imports, future submodule imports from
# controller.py) would open a window. Instead, open the window only when this file
# is executed as a script via the menu action.
#
# Note for future development: if this extension ever adds RoboFont event observers
# (addObserver / addSubscriber), pair them with an uninstall hook so they don't leak
# across extension reloads.

from vfClamp.controller import open_controller


def main():
	"""Open (or focus) the vf-clamp controller window."""
	open_controller()


# RoboFont's path-based menu items execute the file with __name__ == '__main__'.
if __name__ == '__main__':
	main()
