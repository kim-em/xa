"""xa: external amygdala.

A generic monitoring engine. Monitors emit facts; the engine owns policy.

Nothing in this package may know about any particular thing being monitored.
Domain knowledge lives in the policy directory (see xa.config).
"""

__version__ = "0.1.0"
