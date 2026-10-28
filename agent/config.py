"""Which model does what. Only the worker (screen understanding and touch) uses Muse Wonder."""
ORCHESTRATOR_MODEL = "muse-spark-1.3"
WORKER_MODEL = "muse-wonder-1.0"
SLEEP_AFTER_IDLE_MIN = 5
STOP_AFTER_PAUSED_MIN = 60

# Screenshots cost prompt tokens (about 800 at 540 px wide, about 280 at 320 px) but barely change latency (2 to 3 s per call either way).
# Measured on 8 scenarios: 360 px passed as often as 540 px with about 28% fewer prompt tokens, and sending an image every step was
# as accurate as the text-only "adaptive" mode, which was not faster. So the default is an image every step at 360 px.
# "adaptive" attaches one only when it matters (first step, thin UI tree, stuck screen, after an error, and before accepting "done").
import os

IMAGE_MODE = os.environ.get("PHONE_IMAGE", "always")
IMAGE_WIDTH = int(os.environ.get("PHONE_IMAGE_WIDTH", "360"))
