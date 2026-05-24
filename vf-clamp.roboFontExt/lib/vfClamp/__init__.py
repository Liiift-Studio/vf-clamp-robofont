# vf-clamp RoboFont extension entry point — registers the menu item and launches the controller.


def openVFClampController():
	"""Open the vf-clamp controller window."""
	from vfClamp.controller import VFClampController
	VFClampController()
