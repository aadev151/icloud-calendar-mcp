"""Exceptions raised by the calendar service.

They are translated to MCP tool errors and HTTP status codes at the edges.
"""


class CalendarError(Exception):
    status_code = 500


class InvalidInput(CalendarError):
    status_code = 400


class NotFound(CalendarError):
    status_code = 404


class Conflict(CalendarError):
    status_code = 409


class Forbidden(CalendarError):
    status_code = 403


class UpstreamError(CalendarError):
    """The CalDAV server returned an unexpected response."""

    status_code = 502
