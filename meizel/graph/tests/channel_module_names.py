# meiva:note
# The child of `test_ambient_surface._live_names`: print the names the channel modules define.
# It imports the standard library only, so that it runs in a clean `python -I` interpreter, without
# this repository's packages, and a library imported by a test cannot add names to the modules it
# reports on. The modules it reports on are spelled as static imports; the test compares their
# names with `CHANNEL_MODULES`.

from __future__ import annotations

import datetime
import email.utils
import fileinput
import getpass
import grp
import inspect
import json
import locale
import multiprocessing
import netrc
import os
import os.path as os_path
import pathlib
import platform
import pwd
import random
import resource
import secrets
import shutil
import site
import socket
import ssl
import sys
import sysconfig
import tempfile
import threading
import time
import types
import uuid
import zoneinfo

_MODULES: dict[str, types.ModuleType] = {
    "datetime": datetime,
    "email.utils": email.utils,
    "fileinput": fileinput,
    "getpass": getpass,
    "grp": grp,
    "locale": locale,
    "multiprocessing": multiprocessing,
    "netrc": netrc,
    "os": os,
    "os.path": os_path,
    "pathlib": pathlib,
    "platform": platform,
    "pwd": pwd,
    "random": random,
    "resource": resource,
    "secrets": secrets,
    "shutil": shutil,
    "site": site,
    "socket": socket,
    "ssl": ssl,
    "sys": sys,
    "sysconfig": sysconfig,
    "tempfile": tempfile,
    "threading": threading,
    "time": time,
    "uuid": uuid,
    "zoneinfo": zoneinfo,
}


def names_of(modules: dict[str, types.ModuleType]) -> dict[str, list[str]]:
    return {
        name: [
            member
            for member in dir(module)
            if not inspect.ismodule(getattr(module, member)) and getattr(module, member) is not None
        ]
        for name, module in modules.items()
    }


if __name__ == "__main__":
    print(json.dumps(names_of(_MODULES)))
