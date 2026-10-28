"""Moved to `viewer_kit.mailbox`. This name stays an alias of that module, so
older imports and monkeypatches of `review_viewer.mailbox` reach the same objects."""

import sys

import viewer_kit.mailbox

sys.modules[__name__] = viewer_kit.mailbox
