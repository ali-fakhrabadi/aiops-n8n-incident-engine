"""Event topics on the incident stream, in pipeline order."""

TOPIC_OPENED = "incident.opened"  # API -> worker: collect the log context
TOPIC_CONTEXT_READY = "incident.context_ready"  # worker -> worker: analyse it
TOPIC_RESOLVED = "incident.resolved"  # API -> (phase 4) notification of the all-clear
