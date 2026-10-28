"""Moved to `viewer_kit.netinfo`. This name stays an alias of that module, so
older imports and monkeypatches of `review_viewer.netinfo` reach the same objects."""

import sys

import viewer_kit.netinfo

sys.modules[__name__] = viewer_kit.netinfo
