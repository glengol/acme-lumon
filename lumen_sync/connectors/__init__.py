"""Registry of source apps. Adding an app = one connector module + one line here."""

from .lumen import LumenConnector

CONNECTORS = {
    LumenConnector.source_app: LumenConnector,
}
