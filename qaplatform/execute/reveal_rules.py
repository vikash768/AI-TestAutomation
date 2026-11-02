"""Rules shared by the modules that click controls to discover what they open."""
import re

# Never clicked to "see what opens": a confirmation it shows could be accepted by a later
# click, and these actions push data outward or destroy/disable records.
NEVER_OPEN_RE = re.compile(r"\b(publish|release|deploy|push|roll ?out|go live|send|broadcast|delete|remove|destroy|purge|retire|revoke|disable|archive)\b", re.I)
