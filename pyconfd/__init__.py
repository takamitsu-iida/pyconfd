"""
pyconfd - Python implementation of ConfD-equivalent functionality.

Provides:
  - NETCONF server (RFC 6241) over TCP
  - CLI server (Telnet ベース, C-style / J-style)
  - CDB (Configuration Database) with running/candidate/startup datastores
  - CDB subscription mechanism
  - MAAPI (Management Agent API) for Python programs
  - Basic YANG parser
"""

__version__ = "0.1.0"
__author__ = "pyconfd contributors"

from .netconf_server import NetconfServer
from .netconf_ssh_server import NetconfSSHServer
from .cli_server import CLIServer
from .maapi import MAAPI, Transaction
from .cdb import CDB
from .yang_parser import load_yang, YangSchemaRegistry
from .scenario import ScenarioMatcher

__all__ = [
    "NetconfServer",
    "NetconfSSHServer",
    "CLIServer",
    "CDB",
    "MAAPI",
    "Transaction",
    "load_yang",
    "YangSchemaRegistry",
    "ScenarioMatcher",
]
