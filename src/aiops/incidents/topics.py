"""Event topics on the incident stream, in pipeline order."""

TOPIC_OPENED = "incident.opened"  # API -> worker: collect the log context
TOPIC_CONTEXT_READY = "incident.context_ready"  # worker -> worker: analyse it
TOPIC_ANALYZED = "incident.analyzed"  # worker -> worker: notify (analysis done or failed)
TOPIC_RESOLVED = "incident.resolved"  # API -> worker: notify the all-clear
