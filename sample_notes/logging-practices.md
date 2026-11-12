# Logging practices

Log events, not prose: a stable message plus structured fields (request id, duration).
Use levels consistently: ERROR needs a human, WARNING is odd but handled, INFO is
normal milestones, DEBUG is for development. Never log secrets or personal data.
Propagate a correlation id across services so one request can be traced end to end.
